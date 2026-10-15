"""生产进程入口：装配六模块、起 asyncio 循环、挂 HTTP。main.py 仅作纯 Agent 调试入口。

单进程单事件循环（plan Decision 1）：
- Container 装配全部组件，共享一个 asyncpg 池
- asyncio.TaskGroup 编排：WorkerPool 主循环 + Outbox 派发循环 + uvicorn HTTP
- AgentRunServer.as_fastapi_app() 取出 FastAPI 实例，uvicorn.Server.serve() 协程挂载
- 每个长驻协程顶层自带兜底，异常不回抛 TaskGroup

HTTP 的 invoke_agent 惰性引用 utils.create_agent，仅在请求到达时才需要模型凭据。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.container import Container, build_capability, build_channel, jobstore_url_for
from app.contracts.errors import error_digest
from app.platform.observability.alerting import AlertKind, alert
from app.runtime.worker.queue import PersistentTaskSink

logger = logging.getLogger(__name__)

OUTBOX_DISPATCH_INTERVAL_SECONDS = 1.0  # Outbox 派发循环节奏（扫描 not_before 到期行）


class _InboundMarkingSink:
    """投递前记录群激活时刻（Container.mark_inbound），再交给真正的 TaskSink。

    Outbox 的会话态校验靠这条记录判断群是否激活；缺了它，所有出站消息
    都会以 not_activated 被丢弃。
    """

    def __init__(self, container: Container, inner: Any) -> None:
        self._container = container
        self._inner = inner

    async def submit(self, event: Any, context: Any) -> Any:
        if context.chat_id:
            await self._container.mark_inbound(str(context.chat_id))
        return await self._inner.submit(event, context)


async def _outbox_dispatch_loop(
    container: Container, *, interval: float, stopping: asyncio.Event
) -> None:
    """周期调用 dispatch_due 的后台协程；单轮失败只告警不退出（Decision 1）。"""
    while not stopping.is_set():
        try:
            await container.dispatcher.dispatch_due()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            alert(
                AlertKind.FAILURE,
                "Outbox 派发轮次异常",
                error_digest(exc),
                workspace_id=container.workspace_id,
            )
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        except (TimeoutError, asyncio.TimeoutError):
            pass


def build_http_server(container: Container, *, host: str, port: int) -> Any:
    """把 AgentRun HTTP 入口装配成 uvicorn.Server（不启动，由调用方 serve()）。"""

    async def invoke_agent(request: Any):
        """HTTP 直达 Agent 的调试/控制台入口；生产主链路是 gateway 落 task 表。

        惰性引用 utils.create_agent：无模型凭据时启动不受影响，请求到达才报错。
        """
        try:
            from utils import convert_agentrun_messages_to_agentscope, create_agent

            messages = convert_agentrun_messages_to_agentscope(
                getattr(request, "messages", []) or []
            )
            agent = create_agent(thread_id=f"http:{container.workspace_id}")
            result = await agent.reply(messages)
            text = result.get_text_content() if result else ""
            if text:
                yield text
        except Exception as exc:  # noqa: BLE001
            logger.error("invoke_agent 失败: %s", exc, exc_info=True)
            yield f"agent 执行失败: {error_digest(exc)}"

    import config as _config

    import uvicorn
    from agentrun.server import AgentRunServer

    server = AgentRunServer(
        invoke_agent=invoke_agent,
        memory_collection_name=_config.get("MEMORY_COLLECTION_NAME"),
    )
    app = server.as_fastapi_app()
    return uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )


async def serve(
    workspace_id: str | None = None,
    *,
    container: Container | None = None,
    with_http: bool = True,
    http_host: str | None = None,
    http_port: int | None = None,
    outbox_interval: float = OUTBOX_DISPATCH_INTERVAL_SECONDS,
    ready: asyncio.Event | None = None,
    **container_kwargs: Any,
) -> None:
    """进程主入口：装配 → 启动恢复 → TaskGroup 编排长驻协程，直到被取消。

    ready：可选事件，全部组件启动完成后置位（测试与 supervisor 探活用）。
    """
    import config

    config.init()

    owns_container = container is None
    capability = None
    if container is None:
        dsn = container_kwargs.pop("dsn", None) or config.get("PG_DSN")
        ws = workspace_id or config.get("WORKSPACE_ID")
        # 外部能力 Port 在这里接线：repo_sync / tapd_sync 等 Job 经 require_port
        # 取到的就是下面的真实实现；某路探测失败则该键缺省，容器绑
        # NotProvidedPort 占位，Job 失败语义保持诚实（NFR-002）。
        ports: dict[str, Any] = {}
        try:
            capability = await build_capability(ws)
            ports = {
                k: v
                for k, v in {
                    "document": capability.documents,
                    "requirement": capability.requirements,
                    "todo": capability.todos,
                    "repo": capability.code,
                }.items()
                if v is not None
            }
        except Exception as exc:  # noqa: BLE001 能力装配失败不拖垮整个进程
            logger.warning("Capability 装配失败，外部 Port 全部按未绑定处理：%s", exc)
        container = await Container.build(
            ws,
            dsn=dsn,
            jobstore_url=jobstore_url_for(dsn) if dsn else None,
            ports=ports,
            capability=capability,
            **container_kwargs,
        )
    # 启动恢复：PENDING 任务重新入队（US3-5），错过轮次按宽限期补跑（US2-7/9）
    restored = await container.queue.restore_pending(container.task_repo)
    container.scheduler.start()
    recovered = await container.scheduler.recover_all()
    logger.info(
        "wetag[%s] 启动完成：恢复 PENDING=%d，错过轮次处理=%s",
        container.workspace_id, restored, recovered,
    )

    # Channel 装在 Runtime 之后：它的 TaskSink 要拿 Runtime 的 task 表与队列，
    # 入站事件落库即完成去重（裁决 #6），Channel 自己不留任何待处理状态。
    channel = build_channel(
        container.pool,
        _InboundMarkingSink(
            container, PersistentTaskSink(container.task_repo, container.queue)
        ),
        # 事项路由 L3 语义判定：指代消息（"这条需求"）归入既有事项，
        # 不再一律新建 topic 导致 Agent 每轮零记忆开局
        topic_classifier=(
            capability.topic_classifier if capability is not None else None
        ),
        # 审批通过的任务重入队后直接推回内存队列执行，不等重启
        task_queue=container.queue,
        # 审批卡片的需求标题解析（台账/TAPD 端口在 capability 上）
        capability=capability,
    )
    # 真实发送原语在 Channel 装配后才存在；补上后 Outbox 派发才能真正发消息
    container.dispatcher.bind_sender(channel.sender.send_outbox)
    # 流式回复原语同理：注入后 FastAck 优先走 response_url 流式通道（原地更新），
    # 回复窗口过期由发送侧自动降级主动推送
    container.worker_pool.bind_stream_reply(channel.sender.reply_stream)

    # 工作记忆落 PG：重启后多轮对话上下文不丢（此前进程内 InMemoryMemory 重启即空）
    if container.pool is not None:
        from app.platform.memory.working import PgMemory
        from utils import bind_working_memory_factory

        bind_working_memory_factory(
            lambda thread_id: PgMemory(container.pool, thread_id)
        )

    stopping = asyncio.Event()
    http_server = None
    if with_http:
        import config as _config

        host = http_host or _config.get("WETAG_HTTP_HOST", "0.0.0.0")
        port = int(http_port or _config.get("WETAG_HTTP_PORT", 9000))
        http_server = build_http_server(container, host=host, port=port)

    try:
        await channel.start()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(container.worker_pool.run())
            tg.create_task(
                _outbox_dispatch_loop(container, interval=outbox_interval, stopping=stopping)
            )
            if http_server is not None:
                tg.create_task(http_server.serve())
            if ready is not None:
                ready.set()
            try:
                await stopping.wait()  # 长驻：直到被取消（SIGTERM / 测试收尾）
            finally:
                stopping.set()
                container.worker_pool.stop()
                await channel.stop()
                container.scheduler.shutdown(wait=False)
                if http_server is not None:
                    http_server.should_exit = True
    finally:
        if capability is not None:
            await capability.close()
        if owns_container:
            await container.close()


def main() -> None:
    """`python3 -m app.bootstrap`：生产进程入口（supervisor 的 command）。"""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logger.info("收到退出信号，进程收尾")


if __name__ == "__main__":
    main()
