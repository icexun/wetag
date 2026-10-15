"""依赖装配容器：按 ``WORKSPACE_ID`` 构造各模块实例与 Ports 绑定。

一进程一个实例（D-2：一业务线一进程）。这个文件是整个 Capability 模块里**唯一**
知道"今天哪条外部依赖活着"的地方，也是唯一做实现选择的地方。

## 为什么探测放在装配期而不是每次调用

每次调用都探测会把一次外部抖动变成一串重试风暴，也让同一轮任务前后看到的端口
可用性不一致。

所以：装配期探测一次，结论连同**理由**记进 :class:`PortStatus`，整个进程生命周期
内不变。要换实现就重启进程——这对一业务线一进程的部署形态是廉价操作。

## 不可用的端口是 ``None``，不是空实现

这是本文件最容易被"优化"掉的一条。给不可用的端口塞一个"什么都返回空"的实现，
调用方代码会变干净：不用判 None，不用传 ``*_enabled`` 标志。代价是**检测链路会
把"没跑"和"跑了没查到"混成同一个结果**，最终渲染成"一切正常"。

于是这里的约定是：

* 端口不可用 → 字段为 ``None``，并且在 :attr:`CapabilityContainer.statuses` 里
  留一条带理由的记录；
* 需要它的链路拿到 ``*_enabled=False``，自己产出 ``completed=False`` 或在
  ``coverage_note`` 里明示这一维没跑（§9.3）；
* 谁都不许把 ``None`` 静默当成"没有结果"。

:meth:`CapabilityContainer.require` 存在的意义就是让"忘记判 None"变成一个带端口
名和原因的异常，而不是 ``AttributeError: 'NoneType' object has no attribute``。

## 今日的三条已知缺口（计划 R1 / R2 / R4）

装配后 :meth:`CapabilityContainer.coverage_note` 会把它们原样吐出来，供群内卡片
直接引用——Demo 话术里要承认的东西，代码里本来就得有。

## Channel / Space 的装配

``build_channel`` 与 :class:`CapabilityContainer` 并列而不嵌套：Channel 的依赖在
进程启动时就全部确定（长连接凭据、DB 池），没有"探测后选实现"这一层，也不该被
Capability 的端口可用性影响——Capability 全挂时 Channel 仍要能收消息并回一句
"能力暂不可用"。两者由 ``app/bootstrap.py`` 分别装配后接在一起。

装配是唯一允许同时认识多个模块的地方——模块之间只通过 ``contracts/`` 的 Protocol
相识，谁实现谁在这里绑定。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

import config

from app.contracts.errors import PortUnavailable
from app.contracts.ids import WorkspaceId
from app.contracts.job_result import JobResult
from app.contracts.ports import TopicClassifier, WorkspaceConfig, parse_integrations
from app.contracts.task import AGENT_JOB_NAME, TaskSink
from app.gateway.actions import CardActionHandler
from app.gateway.cards.binding import CardBinding
from app.gateway.client import WecomClient
from app.gateway.feedback import FeedbackHandler
from app.gateway.inbound import InboundDispatcher, WelcomeHandler
from app.gateway.outbound import MessageSender
from app.gateway.reply_window import ReplyWindow
from app.gateway.repository import CardTaskRepository, FeedbackRepository
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import ALL_COMMANDS
from app.platform.db import Database
from app.platform.db.migration_check import verify_schema_version
from app.platform.db.pool import Pool, create_pool
from app.runtime.audit import AuditWriter
from app.runtime.jobs.base import ApprovalExpiryJob, JobContext
from app.runtime.outbox.dispatcher import Dispatcher
from app.runtime.outbox.merger import Merger
from app.runtime.outbox.session_guard import SessionGuard
from app.runtime.outbox.throttle import Throttle
from app.runtime.repository import OutboxRepository, TaskRepository
from app.runtime.scheduler.engine import SchedulerEngine
from app.runtime.scheduler.registry import JobRegistry, discover_jobs
from app.runtime.state import RuntimeStateRepository
from app.runtime.worker.agent_runner import AgentRunner
from app.runtime.worker.lease import LeaseKeeper
from app.runtime.worker.pool import WorkerPool
from app.runtime.worker.queue import TaskQueue
from app.space.config_assembler import ConfigVersionProvider
from app.space.context_factory import SpaceContextFactory
from app.space.registry import ChatRegistry
from app.space.repository import SpaceRepository
from app.space.resolver import BotBindingRegistry
from app.space.topic.router import TopicRouter

logger = logging.getLogger(__name__)

# AGENT_JOB_NAME 唯一定义在 contracts/task.py（Channel 与装配层共用），此处仅 re-export
__all__ = ["AGENT_JOB_NAME"]

#: 本进程消费的四个外部 Integration Port（实现归 Capability，contracts/ports.py）
DEFAULT_PORT_NAMES = ("document", "requirement", "todo", "repo")

# 群最后入站时刻在 runtime_state 的键前缀（gateway/Space 入站路径经 mark_inbound 写入）
LAST_INBOUND_KEY_PREFIX = "chat:last_inbound:"


class NotProvidedPort:
    """缺失外部 Port 的显式占位：任何属性访问都抛 PortUnavailable（NFR-002）。

    与「不绑定」的区别：不绑定时 require_port 在 Job 内才报错；占位对象让
    缺失在调用点即显式失败，且容器装配清单里可见哪些 Port 是替身。
    """

    def __init__(self, name: str) -> None:
        self._port_name = name

    @property
    def port_name(self) -> str:
        return self._port_name

    def __getattr__(self, item: str) -> Any:
        raise PortUnavailable(
            f"外部能力 {self._port_name!r} 未绑定（调用 {item}）：任务未完成（外部能力不可用）"
        )

    def __repr__(self) -> str:
        return f"<NotProvidedPort {self._port_name!r}>"


def bind_ports(
    provided: Mapping[str, Any] | None = None,
    *,
    required: Iterable[str] = DEFAULT_PORT_NAMES,
) -> dict[str, Any]:
    """按清单绑定外部 Port：有实现用实现，缺失绑 NotProvidedPort 显式占位。"""
    provided = dict(provided or {})
    bound = {
        name: provided.pop(name) if name in provided else NotProvidedPort(name)
        for name in required
    }
    bound.update(provided)  # 清单外的额外绑定（如 capability:<name>）原样透传
    return bound


def jobstore_url_for(dsn: str) -> str:
    """从 asyncpg DSN 推导 APScheduler jobstore 的同步驱动 DSN（plan Decision 3）。"""
    for scheme in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return "postgresql+psycopg://" + dsn[len(scheme):]
    raise ValueError(f"无法从 DSN 推导 jobstore URL: {dsn!r}")


class Container:
    """单业务线进程的依赖装配结果。所有组件共享一个 asyncpg 池（plan Decision 1）。"""

    def __init__(
        self,
        *,
        workspace_id: str,
        pool: Pool,
        task_repo: TaskRepository,
        outbox_repo: OutboxRepository,
        state: RuntimeStateRepository,
        audit: AuditWriter,
        queue: TaskQueue,
        lease_keeper: LeaseKeeper,
        dispatcher: Dispatcher,
        session_guard: SessionGuard,
        agent_runner: AgentRunner,
        registry: JobRegistry,
        worker_pool: WorkerPool,
        scheduler: SchedulerEngine,
        ports: dict[str, Any],
    ) -> None:
        self.workspace_id = workspace_id
        self.pool = pool
        self.task_repo = task_repo
        self.outbox_repo = outbox_repo
        self.state = state
        self.audit = audit
        self.queue = queue
        self.lease_keeper = lease_keeper
        self.dispatcher = dispatcher
        self.session_guard = session_guard
        self.agent_runner = agent_runner
        self.registry = registry
        self.worker_pool = worker_pool
        self.scheduler = scheduler
        self.ports = ports

    @classmethod
    async def build(
        cls,
        workspace_id: str | None = None,
        *,
        dsn: str | None = None,
        ports: Mapping[str, Any] | None = None,
        sender: Callable[..., Awaitable[str]] | None = None,
        agent_factory: Callable[..., Any] | None = None,
        memory_backend: Any = None,
        topic_rules: Callable[..., Awaitable[str | None]] | None = None,
        auto_rules: Any = None,
        last_inbound_at: Callable[[str], Awaitable[datetime | None]] | None = None,
        jobstore_url: str | None = None,
        verify_schema: bool = True,
        pool_min_size: int = 2,
        pool_max_size: int = 10,
        capability: Any = None,
    ) -> "Container":
        """按业务线装配全部组件；schema 漂移或关键配置缺失时显式报错，不静默降级。"""
        from config.properties import ControlProperties, OutboxProperties, RuntimeProperties

        ws = workspace_id or config.get("WORKSPACE_ID")
        if not ws:
            raise ValueError("WORKSPACE_ID 未配置：进程与业务线 1:1，无法装配")
        dsn = dsn or config.get("PG_DSN")
        if not dsn:
            raise ValueError("PG_DSN 未配置：无法建立业务线连接池")

        pool = await create_pool(dsn, ws, min_size=pool_min_size, max_size=pool_max_size)
        if verify_schema:
            async with pool.acquire() as conn:
                # 漂移即拒绝启动（US1-4）：版本号 + 迁移文件 checksum 双比对，
                # 只查本业务线——同库里别条业务线的漂移不该拦住这个进程。
                await verify_schema_version(conn, workspace_id=ws)

        runtime_props = RuntimeProperties.instance()
        outbox_props = OutboxProperties.instance()
        control_props = ControlProperties.instance()

        # ---- 仓储面 ----
        task_repo = TaskRepository(pool)
        outbox_repo = OutboxRepository(pool)
        state = RuntimeStateRepository(pool)
        audit = AuditWriter(pool)
        queue = TaskQueue()
        lease_keeper = LeaseKeeper(task_repo, queue=queue)

        # ---- Outbox 四道关卡 ----
        merger = Merger(outbox_repo, window_seconds=float(outbox_props.merge_window_seconds))
        if last_inbound_at is None:
            last_inbound_at = _state_last_inbound_provider(state)
        guard = SessionGuard(
            outbox_repo,
            last_inbound_at,
            enforce=bool(outbox_props.session_window_enforce),
            window_hours=float(outbox_props.session_window_hours),
            retention_days=int(outbox_props.suspend_retention_days),
        )
        throttle = Throttle(
            per_minute=int(outbox_props.rate_per_minute),
            per_hour=int(outbox_props.rate_per_hour),
            urgent_per_minute=int(outbox_props.urgent_rate_per_minute),
            urgent_per_hour=int(outbox_props.urgent_rate_per_hour),
        )
        dispatcher = Dispatcher(
            outbox_repo,
            merger=merger,
            guard=guard,
            throttle=throttle,
            sender=sender,
            send_max_attempt=int(outbox_props.send_max_attempt),
        )

        # ---- Control：Rules / 审批 / 记忆 → AgentRunner ----
        from app.control.approval.auto_mode import AutoModeMatcher
        from app.control.approval.gate import ApprovalGate
        from app.control.approval.pending import PendingApprovalRepository
        from app.control.ledger.todo_ref import TodoRefRepository
        from app.control.memory.backend import AgentRunMemoryBackend
        from app.control.memory.collections import MemoryCollections
        from app.control.memory.remember import MemorySedimenter
        from app.control.repository import RulesRepository
        from app.control.rules.assembler import RulesAssembler
        from app.control.rules.middleware import RulesMiddleware

        approval_repo = PendingApprovalRepository(pool)
        todo_ref_repo = TodoRefRepository(pool)
        rules = RulesMiddleware(
            RulesAssembler(RulesRepository(pool), topic_rules=topic_rules),
            max_chars=int(control_props.rules_max_chars),
        )
        # Auto Mode 规则来源：显式入参优先（测试注入），否则走 ControlProperties
        effective_auto_rules = (
            auto_rules if auto_rules is not None else control_props.auto_approve_rules
        )
        auto_matcher = AutoModeMatcher(effective_auto_rules, workspace_id=ws)
        if effective_auto_rules:
            logger.info(
                "Auto Mode 自动批准规则已装配：%s（解析失败=%s）",
                effective_auto_rules, auto_matcher.error,
            )
        gate = ApprovalGate(
            approval_repo,
            auto_matcher=auto_matcher,
            audit=audit,
            outbox_submit=dispatcher.submit,
            timeout_minutes=float(control_props.approval_timeout_minutes),
        )
        memory = MemorySedimenter(
            MemoryCollections(ws),
            memory_backend if memory_backend is not None else AgentRunMemoryBackend(),
        )
        runner_kwargs: dict[str, Any] = {
            "rules": rules,
            "approval_gate": gate,
            "memory": memory,
        }
        agent_runner = AgentRunner(agent_factory, **runner_kwargs) if agent_factory else AgentRunner(**runner_kwargs)

        # ---- Job 注册表 + WorkerPool + 调度 ----
        bound_ports = bind_ports(ports)
        extras: dict[str, Any] = {
            **bound_ports,
            "task_repo": task_repo,
            "approval_repo": approval_repo,
            "todo_ref_repo": todo_ref_repo,
            "lease_keeper": lease_keeper,
            "outbox_submit": dispatcher.submit,
        }
        approval_expiry = ApprovalExpiryJob(
            approvals=approval_repo, tasks=task_repo, outbox_submit=dispatcher.submit
        )
        registry = discover_jobs(lease_keeper=lease_keeper, approval_expiry=approval_expiry)

        async def _agent_handler(ctx: JobContext) -> JobResult:
            return await agent_runner.run(ctx.task)

        # ---- Capability 接线（可选）--------------------------------------
        # 传入 capability 容器时：
        # 1. tapd_sync 换上带 sync_handler 的实例——需求落台账并推送字段变更；
        # 2. doc_poll 换上带仓储的实例——监听清单并入索引、需求关联与订阅的文档；
        # 3. extras 注入 capability:index_refresh——IndexRefreshJob 骨架经它进
        #    CapabilityRouter.handle_job；
        # 4. WorkflowKind 注册为 pool handler——否则派生任务以 KeyError 收场。
        capability_kinds: list[str] = []
        if capability is not None:
            from app.capability.workflows.tapd_watch import make_tapd_sync_handler
            from app.platform.degradation import FreshnessTracker
            from app.runtime.capability_bridge import register_capability
            from app.runtime.jobs.doc_poll import DocPollJob as _DocPollJob
            from app.runtime.jobs.tapd_sync import TapdSyncJob as _TapdSyncJob

            registry.register(
                _TapdSyncJob(sync_handler=make_tapd_sync_handler(capability))
            )
            registry.register(
                _DocPollJob(
                    index_repository=capability.repository,
                    freshness=FreshnessTracker(state),
                )
            )
            extras["capability_repository"] = capability.repository
            capability_bot_id = config.get("WECOM_BOT_ID") or "system"

            # 群聊改文档的即时触发通道：agent 工具写成功后直接派生
            # index_refresh（落库+入队，与 pool._on_success 的派生路径同型）。
            from app.capability.agents import lark_office_tools as _lark_office_tools
            from app.capability.agents import lark_tools as _lark_tools
            from app.capability.agents import subscription_tools as _subscription_tools
            from app.capability.agents import tapd_tools as _tapd_tools
            from app.capability.agents import wecom_office_tools as _wecom_office_tools
            from app.capability.agents import wecom_tools as _wecom_tools

            async def _derive_from_chat_write(spec: Any) -> None:
                created = await task_repo.create(spec)
                if created is not None:
                    queue.push(created)

            _lark_tools.bind_derived_sink(_derive_from_chat_write)
            _wecom_tools.bind_dependencies(capability.todos, todo_ref_repo)
            _subscription_tools.bind_dependencies(capability.repository)
            _tapd_tools.bind_dependencies(
                capability.cli, capability.workspace.tapd_project_id
            )
            _lark_office_tools.bind_dependencies(capability.cli)
            _wecom_office_tools.bind_dependencies(capability.cli)

            # 集成未启用或 CLI 缺失：对应端口为空，其轮询 Job 不起
            for port_name, job_name in (
                ("requirements", "tapd_sync"),
                ("documents", "doc_poll"),
                ("todos", "todo_readback"),
            ):
                if getattr(capability, port_name, None) is None and registry.unregister(job_name):
                    logger.info("%s 不可用，不调度 %s", port_name, job_name)

        handlers: dict[str, Any] = {job.name: job for job in registry.all()}
        handlers[AGENT_JOB_NAME] = _agent_handler
        if capability is not None:
            capability_kinds = register_capability(
                handlers, extras, capability,
                workspace_id=ws, bot_id=capability_bot_id,
            )
            logger.info("Capability 工作流已接线：%s", capability_kinds)

        worker_pool = WorkerPool(
            task_repo,
            handlers,
            queue=queue,
            state=state,
            outbox_submit=dispatcher.submit,
            workers=int(runtime_props.workers),
            agentic_timeout=float(runtime_props.agentic_task_timeout),
            deterministic_timeout=float(runtime_props.deterministic_task_timeout),
            fail_streak_threshold=int(runtime_props.fail_alert_streak),
            process_bot_id=config.get("WECOM_BOT_ID") or None,
            ports=extras,
            fastack_seconds=float(runtime_props.fast_ack_seconds),
            fastack_progress_interval=float(runtime_props.progress_interval_seconds),
        )
        scheduler = SchedulerEngine(
            ws,
            task_repo=task_repo,
            queue=queue,
            state=state,
            jobstore_url=jobstore_url,
            misfire_grace_seconds=int(runtime_props.misfire_grace_seconds),
        )
        scheduler.register_jobs(registry)

        return cls(
            workspace_id=ws,
            pool=pool,
            task_repo=task_repo,
            outbox_repo=outbox_repo,
            state=state,
            audit=audit,
            queue=queue,
            lease_keeper=lease_keeper,
            dispatcher=dispatcher,
            session_guard=guard,
            agent_runner=agent_runner,
            registry=registry,
            worker_pool=worker_pool,
            scheduler=scheduler,
            ports=bound_ports,
        )

    async def mark_inbound(self, chat_id: str, *, now: datetime | None = None) -> dict[str, int]:
        """群内入站（@ 机器人）接线点：记录激活时刻并补发挂起消息（US5-5）。

        gateway/Space 入站路径在投递 task 前调用本方法。
        """
        now = now or datetime.now(UTC)
        await self.state.set(
            f"{LAST_INBOUND_KEY_PREFIX}{chat_id}", {"last_inbound_at": now.isoformat()}
        )
        return await self.session_guard.resume_on_inbound(chat_id, now=now)

    async def close(self) -> None:
        """进程收尾：先停调度再关池。"""
        self.scheduler.shutdown(wait=False)
        await self.pool.close()


def _state_last_inbound_provider(
    state: RuntimeStateRepository,
) -> Callable[[str], Awaitable[datetime | None]]:
    """默认「群最后入站时刻」提供者：读 runtime_state（由 mark_inbound 写入）。

    返回 None 表示该群从未激活（US5-4 的判定依据）。
    """

    async def _lookup(chat_id: str) -> datetime | None:
        record = await state.get(f"{LAST_INBOUND_KEY_PREFIX}{chat_id}")
        if not record:
            return None
        raw = record.get("last_inbound_at")
        if not raw:
            return None
        ts = datetime.fromisoformat(str(raw))
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

    return _lookup


# 这些环境变量名在 registry 的 ``env_keys`` 里声明过，这里只负责从进程环境取值。
# **取到的值从不落日志、不进 argv、不进审计摘要**（见 cli/result.digest）。
_SECRET_KEYS = (
    "LARK_CLI_TOKEN", "TAPD_TOKEN", "TAPD_API_ENDPOINT", "TAPD_NPC_ROLE", "WECOM_CLI_TOKEN",
)


@dataclass(frozen=True, slots=True)
class PortStatus:
    """一个端口的装配结论。

    ``impl`` 记的是**实际选中的实现**（``lark_cli`` / ``tapd_cli`` / ``none``），
    ``reason`` 在不可用时必填。两者一起构成卡片里"覆盖范围"那段话的原料：
    用户看到"没查到"时会据此决定要不要自己再查一遍，所以理由不能是
    "不可用"三个字。
    """

    name: str
    available: bool
    impl: str = "none"
    reason: str = ""

    def render(self) -> str:
        if self.available:
            # 可用**且带 reason** = 有保留的可用（例如只读、或部分能力缺失）。
            # 这类保留必须照样出现在覆盖范围里：调用方据"取全了"做判断，而这个
            # 结论的强度取决于走的是哪条路。
            caveat = f"，但 {self.reason}" if self.reason else ""
            return f"{self.name}: 可用（{self.impl}）{caveat}"
        return f"{self.name}: 不可用——{self.reason or '未说明'}"

    @property
    def degraded(self) -> bool:
        """可用但有保留。覆盖范围要提，但不影响"能不能跑"。"""
        return self.available and bool(self.reason)


@dataclass(slots=True)
class CapabilityContainer:
    """装配结果。字段为 ``None`` 即该能力今日不存在，见模块 docstring。"""

    workspace: WorkspaceConfig
    cli: CliExecutor
    statuses: tuple[PortStatus, ...] = ()

    # Integration 层
    documents: Any | None = None
    requirements: Any | None = None
    todos: Any | None = None
    code: Any | None = None

    # 持久化
    db: Any | None = None
    repository: Any | None = None
    idempotency: Any | None = None
    # biz 层规则写入面（Loop-3 自动更新 prompt 规约；热更新经 RulesMiddleware 生效）
    rules_writer: Any | None = None
    # todo_ref 归属登记（Control 的表，裁决 #10：Capability 只引用不定义，但需回写）
    todo_refs: Any | None = None

    # Intelligence 层
    agent: Any | None = None
    embedder: Any | None = None
    structuring: Any | None = None
    topic_classifier: Any | None = None

    # 系统通知的推送目标解析（``Callable[[], Awaitable[str | None]]``）：
    # 无会话归属的定时任务（如 Loop 周报）靠它找最近活跃群。
    alert_chat_provider: Any | None = None

    _closers: list[Any] = field(default_factory=list)

    # -- 查询 -------------------------------------------------------------
    def status(self, name: str) -> PortStatus:
        for s in self.statuses:
            if s.name == name:
                return s
        return PortStatus(name, False, reason="未装配")

    def require(self, name: str) -> Any:
        """取一个必须可用的组件，不可用即抛带理由的 :class:`PortUnavailable`。

        存在的唯一理由是让"忘记判 None"以正确的形态失败：``AttributeError`` 在
        日志里看不出是哪条外部依赖没起来，而这里抛出来的异常自带端口名与原因，
        可以直接进群内文案。
        """
        value = getattr(self, name, None)
        if value is None:
            st = self.status(name)
            raise PortUnavailable(name, "PORT_NOT_ASSEMBLED", st.reason or "未装配")
        return value

    def coverage_note(self) -> str:
        """不可用能力的人话汇总，直接进卡片的"覆盖范围"段（§9.3）。

        返回空串表示全都可用。**调用方不得在这一段为空时才展示**——它为空正是
        "没查到"可信的前提，非空时那句结论必须被抑制或加限定。
        """
        missing = [s for s in self.statuses if not s.available or s.degraded]
        if not missing:
            return ""
        return "；".join(s.render() for s in missing)

    async def close(self) -> None:
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:  # noqa: BLE001 关闭失败不该盖掉正在处理的异常
                logger.exception("容器关闭时出错")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


async def build_capability(
    workspace_id: str | None = None,
    *,
    probe: bool = True,
    env: dict[str, str] | None = None,
) -> CapabilityContainer:
    """按进程环境装配 Capability。

    :param probe: 是否真的执行探测命令。测试里置 False——探测要起子进程，而
        单测不该依赖本机装了什么。置 False 时按"二进制存在即可用"乐观装配。
    :param env: 覆盖进程环境，仅供测试。**显式传入时它就是全部环境**——不在其中
        的键视为未配置，见下方 ``_cfg``。
    """
    from config.properties import (
        CapabilityProperties,
        CliProperties,
        PgProperties,
        WorkspaceProperties,
    )

    environ = env if env is not None else dict(os.environ)
    wsp = WorkspaceProperties.instance()
    clip = CliProperties.instance()
    cap = CapabilityProperties.instance()
    pgp = PgProperties.instance()

    def _cfg(key: str, prop_value: Any) -> str | None:
        """取一个"决定某能力是否存在"的配置项。

        ``env`` 显式给出时以它为全部环境：缺的键就是没配。这一条不是洁癖——
        ``Value`` 描述符每次访问都直接读 ``os.environ``，于是 ``env={}`` 的调用方
        会以为自己隔离了环境，实际上 ``PG_DSN`` / ``REPO_ROOT`` 仍从开发机漏进来，
        同一条用例在不同人机器上得出不同结论，而它恰好是在断言"能力不可用"。
        生产路径 ``env is None`` 时行为不变，仍走配置中心 → 默认值。
        """
        if env is not None:
            return environ.get(key)
        return str(prop_value) if prop_value else None

    ws_id = WorkspaceId(workspace_id or wsp.workspace_id or "default")

    # 相对路径的 REPO_ROOT 锚定到项目根，而非进程 cwd：否则从别的目录启动时
    # 同一份 .env 会解析到不同位置，code 维度端口静默不可用。
    repo_root = _cfg("REPO_ROOT", cap.repo_root)
    if repo_root:
        from pathlib import Path

        p = Path(repo_root).expanduser()
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[1] / p
        repo_root = str(p)

    workspace = WorkspaceConfig(
        workspace_id=ws_id,
        tapd_project_id=environ.get("TAPD_PROJECT_ID"),
        repo_root=repo_root,
        repo_branch=cap.repo_branch or "main",
        integrations=parse_integrations(_cfg("INTEGRATIONS", wsp.integrations)),
    )
    logger.info("已启用集成：%s", ", ".join(sorted(workspace.integrations)) or "（无）")

    statuses: list[PortStatus] = []
    container = CapabilityContainer(workspace=workspace, cli=_build_cli(ws_id, clip, environ))

    # --- 语义节点。没有它，结构化与语义路由都只能产出 completed=False -------
    container.agent, agent_status = _build_agent(
        timeout_s=float(cap.llm_timeout_s or 180.0)
    )
    statuses.append(agent_status)

    # --- PG。快照、订阅与幂等都在上面 -------------------------------------
    await _wire_database(container, statuses, pgp, ws_id, dsn=_cfg("PG_DSN", pgp.dsn))

    def _disabled(port: str, name: str) -> bool:
        if name in workspace.integrations:
            return False
        statuses.append(PortStatus(port, False, reason=f"未启用 {name} 集成（INTEGRATIONS）"))
        return True

    # --- 文档口（R3：飞书授权是今日最大单点）-----------------------------
    if not _disabled("documents", "lark"):
        await _wire_documents(container, statuses, cap, probe=probe)

    # --- 需求口：官方 tapd-cli，无替身 ------------------------------------
    if not _disabled("requirements", "tapd"):
        await _wire_requirements(container, statuses, environ, workspace, probe=probe)

    # --- 待办口（R2：无降级，不可用即缺失）-------------------------------
    if not _disabled("todos", "wecom"):
        await _wire_todos(container, statuses, ws_id, probe=probe)

    # --- 代码口（R4：无大仓时退化，但退化本身要被记下来）------------------
    _wire_code(container, statuses, cap, workspace)

    # --- Intelligence + 编排 ---------------------------------------------
    _wire_intelligence(container, statuses, cap)

    container.statuses = tuple(statuses)
    note = container.coverage_note()
    logger.info(
        "Capability 装配完成 ws=%s documents=%s requirements=%s%s",
        ws_id,
        container.documents is not None,
        container.requirements is not None,
        f" 覆盖范围：{note}" if note else "",
    )
    return container


# --- 各段 -------------------------------------------------------------------


def _build_cli(ws_id: str, clip: Any, environ: dict[str, str]) -> CliExecutor:
    """一个 executor 管所有命令。

    凭据在这里从进程环境取，且**只取本进程的**——跨业务线的凭据根本不在这个进程
    的 environ 里（D-2 进程级隔离）。executor 再按每条命令声明的 ``env_keys``
    取子集注入子进程，所以 ``lark`` 的命令拿不到 TAPD 的 token。
    """
    secrets = {k: environ[k] for k in _SECRET_KEYS if environ.get(k)}
    endpoint = getattr(clip, "tapd_endpoint", None)
    if secrets.get("TAPD_TOKEN") and endpoint:
        secrets.setdefault("TAPD_API_ENDPOINT", str(endpoint))
    if secrets:
        # 只打键名，绝不打值
        logger.info("已装载凭据：%s", ", ".join(sorted(secrets)))
    return CliExecutor(
        workspace_id=str(ws_id),
        binaries={
            "lark": clip.lark_bin or "lark-cli",
            "tapd": clip.tapd_bin or "tapd-cli",
            "wecom": clip.wecom_bin or "wecom-cli",
            "rg": clip.rg_bin or "rg",
            "git": clip.git_bin or "git",
        },
        secrets=secrets,
        specs=ALL_COMMANDS,
        timeout_s=float(clip.timeout_s or 30.0),
        max_output_bytes=int(clip.max_output_bytes or 8 * 1024 * 1024),
        max_attempts=int(clip.max_attempts or 3),
    )


def _build_agent(*, timeout_s: float) -> tuple[Any | None, PortStatus]:
    """装配语义节点。

    ``timeout_s`` 从配置来而不是用客户端默认的 90 秒：需求结构化一次要回十几个字段
    外加验收标准，实测会把 90 秒跑穿。而跑穿的表现不是报错，是那个节点 ``ok=False``
    → 整条检测 ``completed=False``——能力看着在，什么都不敢说。
    """
    try:
        from app.capability.agents.factory import create_structured_agent

        agent = create_structured_agent(timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        # 模型不可用不抛：其他能力（索引刷新、代码检索）不依赖它，
        # 只是语义判断全部走 completed=False。
        return None, PortStatus(
            "agent", False, reason=f"结构化输出客户端不可用：{type(exc).__name__}: {exc}"
        )
    return agent, PortStatus("agent", True, impl="structured")


async def _wire_database(
    container: CapabilityContainer,
    statuses: list[PortStatus],
    pgp: Any,
    ws_id: str,
    *,
    dsn: str | None,
) -> None:
    if not dsn:
        statuses.append(
            PortStatus("db", False, reason="PG_DSN 未配置；文档快照、订阅与幂等台账不可用")
        )
        return
    try:
        from app.capability.integration.idempotency import Idempotency
        from app.capability.repository import CapabilityRepository
        from app.platform.db.pool import Database

        db = Database(
            dsn,
            str(ws_id),
            pool_min=int(pgp.pool_min or 2),
            pool_max=int(pgp.pool_max or 10),
            connect_timeout_s=float(pgp.connect_timeout_s or 5.0),
            command_timeout_s=float(pgp.command_timeout_s or 15.0),
            statement_timeout_ms=int(pgp.statement_timeout_ms or 10_000),
            acquire_timeout_s=float(pgp.acquire_timeout_s or 5.0),
            tx_soft_limit_ms=int(pgp.tx_soft_limit_ms or 2_000),
        )
        await db.connect()
    except Exception as exc:  # noqa: BLE001
        statuses.append(PortStatus("db", False, reason=f"PG 连接失败：{exc}"))
        return

    container.db = db
    container.repository = CapabilityRepository(db)
    container.idempotency = Idempotency(db, container.repository)
    from app.control.ledger.todo_ref import TodoRefRepository
    from app.control.repository import RulesRepository

    container.todo_refs = TodoRefRepository(db)
    # Loop-3 自动写 biz 层规约的通道；读取与生效在 Runtime 的 RulesMiddleware
    container.rules_writer = RulesRepository(db)
    container._closers.append(db.close)
    # 系统通知推送目标：最近活跃群（只读 Space 的 chat_group）
    container.alert_chat_provider = container.repository.latest_active_chat_id
    statuses.append(PortStatus("db", True, impl="postgresql"))


async def _wire_documents(
    container: CapabilityContainer,
    statuses: list[PortStatus],
    cap: Any,
    *,
    probe: bool,
) -> None:
    """飞书文档口。

    R3：授权状态是今日最大单点。未授权时 ``fetch`` 全部 ``AUTH_FAILED``，索引全
    stale，变更通知停摆。所以探测失败时端口置 ``None`` 而不是留着让每次调用
    各自失败——后者会把一个已知的、可以在卡片里说清楚的缺口，摊成运行期一串
    看起来互不相关的告警。
    """
    from app.capability.integration.document.lark_cli import LarkDocumentAdapter

    adapter = LarkDocumentAdapter(
        container.cli, fetch_concurrency=int(cap.fetch_concurrency or 3)
    )
    if probe and not container.cli.binary_available("lark.docs.fetch"):
        statuses.append(
            PortStatus("documents", False, reason="lark-cli 不在 PATH 上（R3）")
        )
        return
    container.documents = adapter
    statuses.append(PortStatus("documents", True, impl="lark_cli"))


async def _wire_requirements(
    container: CapabilityContainer,
    statuses: list[PortStatus],
    environ: dict[str, str],
    workspace: WorkspaceConfig,
    *,
    probe: bool,
) -> None:
    """需求口：唯一实现官方 ``tapd-cli``，不可用时如实标记，没有替身。"""
    from app.capability.integration.requirement.tapd_cli import TapdCliAdapter

    if probe and not environ.get("TAPD_TOKEN"):
        statuses.append(
            PortStatus("requirements", False, reason="未配置 TAPD_TOKEN；TAPD 读写均不可用")
        )
        return
    adapter = TapdCliAdapter(
        container.cli,
        workspace_id=workspace.workspace_id,
        project_id=workspace.tapd_project_id,
        idempotency=container.idempotency,
    )
    if probe and not await adapter.probe():
        statuses.append(
            PortStatus(
                "requirements",
                False,
                reason=f"{adapter.probe_error or 'tapd-cli 不可用'}；TAPD 读写均不可用",
            )
        )
        return
    container.requirements = adapter
    statuses.append(PortStatus("requirements", True, impl="tapd_cli"))


async def _wire_todos(
    container: CapabilityContainer,
    statuses: list[PortStatus],
    ws_id: str,
    *,
    probe: bool,
) -> None:
    """待办口。D-1：唯一实现，**没有降级分支**。

    不可用时这里绝不塞任何替身。可选的动作只有一个：把 E-4 / E-5 标为今日不交付，
    并让卡片明示。理由见 ``todo/wecom.py`` 模块 docstring。
    """
    from app.capability.integration.todo.wecom import WecomTodoAdapter

    if probe and not container.cli.binary_available("wecom.todo.create"):
        statuses.append(
            PortStatus(
                "todos",
                False,
                reason="wecom-cli 不在 PATH 上（R2）；E-4/E-5 今日不交付，"
                "不做本地台账替身（D-1）",
            )
        )
        return
    container.todos = WecomTodoAdapter(container.cli, workspace_id=str(ws_id))
    statuses.append(PortStatus("todos", True, impl="wecom_cli"))


def _wire_code(
    container: CapabilityContainer,
    statuses: list[PortStatus],
    cap: Any,
    workspace: WorkspaceConfig,
) -> None:
    """代码口（R4）。

    ``REPO_ROOT`` 未配或不存在 → 端口不可用。**不退化成"搜当前工作目录"**：
    那样代码搜索会在一个与线上代码无关的仓库里搜，产出的 locator 语法完全合法、
    指向的文件真实存在，而结论与线上行为毫无关系。这种证据比没有证据坏。
    """
    from pathlib import Path

    # 取 workspace 上已解析好的那份，不再回头读 cap.repo_root：同一个配置项被读
    # 两次就有两个答案，而 build_capability 的 env 覆盖只作用于其中一次。
    root = workspace.repo_root
    if not root:
        statuses.append(
            PortStatus("code", False, reason="REPO_ROOT 未配置（R4）；代码搜索不可用")
        )
        return
    if not Path(root).is_dir():
        statuses.append(
            PortStatus("code", False, reason=f"REPO_ROOT 不是目录：{root}（R4）")
        )
        return
    if not container.cli.binary_available("rg.search") and not container.cli.binary_available(
        "git.grep"
    ):
        statuses.append(
            PortStatus("code", False, reason="rg 与 git grep 都不可用，无法检索代码")
        )
        return

    from app.capability.integration.repo.service import RepoService

    container.code = RepoService(container.cli, repo_root=root)
    statuses.append(PortStatus("code", True, impl="repo_service"))


def _wire_intelligence(
    container: CapabilityContainer,
    statuses: list[PortStatus],
    cap: Any,
) -> None:
    """Intelligence 与编排。

    每个节点的前置条件都显式判：缺了就置 ``None`` 并记一条理由。写成一串 if 而不是
    try/except 包一大段，是为了让"为什么这条链路今天不跑"在日志里只有一个答案。
    """
    from app.capability.intelligence.structuring import Structuring

    # 文档快照的向量。``impl`` 写明是哪一种：词法向量不是语义向量。
    try:
        from app.capability.intelligence.embedding import build_embedder

        container.embedder = build_embedder(
            model=cap.embedding_model,
            dim=int(cap.embedding_dim or 1024),
            timeout_s=float(cap.embedding_timeout_s or 20.0),
        )
        name = getattr(container.embedder, "name", type(container.embedder).__name__)
        statuses.append(PortStatus("embedder", True, impl=name))
    except Exception as exc:  # pragma: no cover - 装配期兜底
        statuses.append(
            PortStatus("embedder", False, reason=f"Embedder 装配失败（{type(exc).__name__}）")
        )

    if container.agent is not None:
        container.structuring = Structuring(container.agent)
        # 事项路由 L3 语义判定：指代消息（"这条需求"）不再一律新建事项
        from app.capability.intelligence.topic_route import LlmTopicClassifier

        container.topic_classifier = LlmTopicClassifier(container.agent)


__all__ = ["CapabilityContainer", "PortStatus", "build_capability"]


@dataclass
class ChannelRuntime:
    """Channel 与 Space 对外暴露的全部能力。"""

    client: WecomClient
    inbound: InboundDispatcher
    sender: MessageSender
    """Runtime 的 Outbox 通过它发送，这是 Channel 唯一的出站入口。"""
    space: SpaceContextFactory
    chats: ChatRegistry
    cards: CardBinding
    card_tasks: CardTaskRepository
    feedbacks: FeedbackRepository
    bindings: BotBindingRegistry
    reply_window: ReplyWindow

    _purge_task: Optional[asyncio.Task] = None

    @property
    def evicted(self) -> bool:
        return self.client.evicted

    async def start(self) -> None:
        await self.client.start()
        self._purge_task = asyncio.create_task(self.reply_window.run_purge_loop())

    async def stop(self) -> None:
        if self._purge_task is not None:
            self._purge_task.cancel()
            self._purge_task = None
        await self.client.stop()


def _make_story_title_resolver(capability: Any) -> Any:
    """审批卡片的需求标题解析器：本地需求台账优先，TAPD 端口兜底。

    台账是轮询同步的（tapd_sync），新建未及同步的需求走端口实时查；
    两者都拿不到返回 None，卡片回退显示原始 ID——名称是易读性增强，
    解析失败不能拖垮发卡。
    """
    if capability is None:
        return None
    repo = getattr(capability, "repository", None)
    port = getattr(capability, "requirements", None)
    if repo is None and port is None:
        return None

    async def resolve(story_id: str) -> str | None:
        if repo is not None:
            try:
                row = await repo.get_requirement(story_id)
                if row is not None and row["title"]:
                    return str(row["title"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("需求台账查询失败 tapd=%s：%r", story_id, exc)
        if port is not None and hasattr(port, "get_story"):
            try:
                story = await port.get_story(story_id)
                title = getattr(story, "title", None)
                if title:
                    return str(title)
            except Exception as exc:  # noqa: BLE001
                logger.warning("TAPD 需求查询失败 tapd=%s：%r", story_id, exc)
        return None

    return resolve


def build_channel(
    db: Database,
    task_sink: TaskSink,
    topic_classifier: Optional[TopicClassifier] = None,
    config_versions: Optional[ConfigVersionProvider] = None,
    welcome_handler: Optional[WelcomeHandler] = None,
    client: Optional[object] = None,
    task_queue: Optional[object] = None,
    capability: Optional[object] = None,
) -> ChannelRuntime:
    bindings = BotBindingRegistry.from_config()
    binding = bindings.current

    space_repository = SpaceRepository(db)
    space = SpaceContextFactory(
        space_repository,
        bindings,
        TopicRouter(space_repository, topic_classifier),
        config_versions,
    )
    chats = ChatRegistry(space_repository)

    card_tasks = CardTaskRepository(db)
    cards = CardBinding(card_tasks)
    feedbacks = FeedbackRepository(db)

    wecom = WecomClient(client)
    reply_window = ReplyWindow()
    sender = MessageSender(
        client=wecom.raw,
        reply_window=reply_window,
        chat_directory=chats,
        workspace_id=binding.workspace_id,
        bot_id=binding.bot_id,
        # 审批卡片发送时登记安全绑定，按钮点击才查得到、才有回显
        card_tasks=card_tasks,
        # 审批卡片的需求 ID → 标题解析（台账优先、TAPD 端口兜底）
        story_title_resolver=_make_story_title_resolver(capability),
    )

    # 审批决策闭环：按钮 confirm/cancel → pending_approval 状态迁移 + 任务重入队
    from app.control.approval.pending import PendingApprovalRepository
    from app.runtime.repository import TaskRepository

    inbound = InboundDispatcher(
        bot_id=binding.bot_id,
        space=space,
        task_sink=task_sink,
        sender=sender,
        card_actions=CardActionHandler(
            cards,
            space,
            task_sink,
            sender,
            approval_repo=PendingApprovalRepository(db),
            task_repo=TaskRepository(db),
            task_queue=task_queue,
        ),
        feedbacks=FeedbackHandler(cards, feedbacks),
        reply_window=reply_window,
        welcome_handler=welcome_handler,
    )
    wecom.subscribe(inbound.on_message, inbound.on_event)

    return ChannelRuntime(
        client=wecom,
        inbound=inbound,
        sender=sender,
        space=space,
        chats=chats,
        cards=cards,
        card_tasks=card_tasks,
        feedbacks=feedbacks,
        bindings=bindings,
        reply_window=reply_window,
    )
