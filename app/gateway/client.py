"""企微智能机器人长连接客户端，收发原语。

基于官方 ``wecom-aibot-python-sdk`` 建立并维持长连接，把回调帧交给
``inbound`` 的编排器。出站帧由 ``outbound`` 直接用本模块暴露的 ``raw``
客户端发送。

每个 Bot 同时只能有一个有效长连接，新连接会踢掉旧连接。因此高可用只能做
主备切换：备用实例取得连接所有权后才建连，旧实例收到 ``disconnected_event``
后停止重连（见 ``_evict``）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from app.gateway.inbound import event_type_of
from config.properties import WecomProperties

try:
    from agentrun.utils.log import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

FrameHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class WecomClient:
    def __init__(self, client: Optional[Any] = None):
        self._props = WecomProperties.instance()
        self._client = client if client is not None else _build_client(self._props)
        self._message_handler: Optional[FrameHandler] = None
        self._event_handler: Optional[FrameHandler] = None
        self._evicted = False
        """被新连接踢掉。置位后不再重连，进程应退出交出连接所有权。"""

        self._bind_events()

    @property
    def raw(self) -> Any:
        """官方 SDK 客户端。出站原语用它发送，业务层不直接持有。"""
        return self._client

    @property
    def evicted(self) -> bool:
        return self._evicted

    def subscribe(
        self, on_message: FrameHandler, on_event: FrameHandler
    ) -> None:
        self._message_handler = on_message
        self._event_handler = on_event

    async def start(self) -> None:
        await self._client.connect()

    async def stop(self) -> None:
        self._client.disconnect()

    # ===== 事件绑定 =====

    def _bind_events(self) -> None:
        self._client.on("message", self._dispatch_message)
        self._client.on("event", self._dispatch_event)
        self._client.on(
            "authenticated", lambda: logger.info("WeCom long connection authenticated")
        )
        self._client.on(
            "reconnecting",
            lambda attempt: logger.warning("Reconnecting to WeCom (attempt %s)", attempt),
        )
        self._client.on(
            "disconnected",
            lambda reason: logger.warning("WeCom connection lost: %s", reason),
        )
        self._client.on("error", lambda err: logger.error("WeCom client error: %s", err))

    async def _dispatch_message(self, frame: Dict[str, Any]) -> None:
        if self._message_handler is not None:
            await self._message_handler(frame)

    async def _dispatch_event(self, frame: Dict[str, Any]) -> None:
        if event_type_of(frame) == "disconnected_event":
            self._evict()
            return
        if self._event_handler is not None:
            await self._event_handler(frame)

    def _evict(self) -> None:
        """新连接已建立，本连接被服务端踢掉。

        必须停止重连：继续重连会把新实例再踢掉，两个实例互相踢形成抖动。
        """
        self._evicted = True
        logger.warning(
            "Connection evicted by a newer subscriber; stopping reconnect. "
            "This instance should hand over ownership and exit."
        )
        self._client.disconnect()


def _build_client(props: WecomProperties) -> Any:
    from aibot import WSClient, WSClientOptions

    return WSClient(
        WSClientOptions(
            bot_id=props.bot_id,
            secret=props.bot_secret,
            ws_url=props.ws_url or "",
            heartbeat_interval=int(props.heartbeat_interval_ms),
            reconnect_interval=int(props.reconnect_interval_ms),
            max_reconnect_attempts=int(props.max_reconnect_attempts),
            request_timeout=int(props.request_timeout_ms),
        )
    )
