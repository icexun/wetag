"""回复窗口：保存可回复的原始帧。

骨架（§2.3）未给这件事单列文件，但它无法并入别处：长连接下每次回复都必须
透传回调帧的 ``req_id``，而任务是异步执行的——Runtime 拿到 Task 时原始帧早已
出栈。Channel 因此按 ``event_id`` 暂存帧，让出站侧只需持有 ``event_id``。

窗口是平台给的硬约束，不是缓存策略：
- 事件回调（欢迎语、卡片更新）必须在 5 秒内回复；
- 流式消息从首次发送起 10 分钟内必须 ``finish``，否则自动结束。

窗口过期后不是失败，而是降级为主动推送（``aibot_send_msg``）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:
    from agentrun.utils.log import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

EVENT_REPLY_TTL_SECONDS = 5.0
MESSAGE_REPLY_TTL_SECONDS = 600.0
PURGE_INTERVAL_SECONDS = 60.0


@dataclass
class _Entry:
    frame: Dict[str, Any]
    expires_at: float
    stream_id: str = ""
    stream_finished: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class ReplyWindow:
    def __init__(self, clock=time.monotonic):
        self._entries: Dict[str, _Entry] = {}
        self._clock = clock

    def register(
        self,
        event_id: str,
        frame: Dict[str, Any],
        ttl_seconds: float = MESSAGE_REPLY_TTL_SECONDS,
        stream_id: str = "",
    ) -> None:
        self._entries[event_id] = _Entry(
            frame=frame,
            expires_at=self._clock() + ttl_seconds,
            stream_id=stream_id,
        )

    def frame_of(self, event_id: str) -> Optional[Dict[str, Any]]:
        entry = self._get(event_id)
        return entry.frame if entry else None

    def stream_id_of(self, event_id: str) -> Optional[str]:
        """同一条流式消息的多次刷新必须复用同一个 ``stream.id``。"""
        entry = self._get(event_id)
        return entry.stream_id if entry else None

    def set_stream_id(self, event_id: str, stream_id: str) -> None:
        entry = self._get(event_id)
        if entry:
            entry.stream_id = stream_id

    def finish_stream(self, event_id: str) -> None:
        """流式消息置为完成后立即释放，避免持有已不可用的帧。"""
        entry = self._get(event_id)
        if entry:
            entry.stream_finished = True
        self._entries.pop(event_id, None)

    def discard(self, event_id: str) -> None:
        self._entries.pop(event_id, None)

    def purge_expired(self) -> int:
        now = self._clock()
        expired = [k for k, v in self._entries.items() if v.expires_at <= now]
        for key in expired:
            del self._entries[key]
        return len(expired)

    async def run_purge_loop(
        self, interval_seconds: float = PURGE_INTERVAL_SECONDS
    ) -> None:
        """后台清理过期条目。由装配层起成 task，清理失败不影响主循环。"""
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                purged = self.purge_expired()
                if purged:
                    logger.debug("Purged %d expired reply windows", purged)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                logger.warning("Reply window purge failed: %s", exc)

    def __len__(self) -> int:
        return len(self._entries)

    def _get(self, event_id: str) -> Optional[_Entry]:
        entry = self._entries.get(event_id)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[event_id]
            return None
        return entry
