"""记忆后端抽象：托管 collection 的读写面。

生产绑定 AgentRun 托管 collection（`AgentRunMemoryBackend`，try-import 可选降级）；
本地无平台凭据时后端不可用，一切异常翻译为 `MemoryUnavailable`，由调用侧
转为显式降级（US8-9）。测试注入内存替身。

文档级 search/upsert 的 SDK 调用面按命名推断（explore.md 待确认 #1/#2），
接入真实平台凭据后需校准一次。
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from app.contracts.errors import MemoryUnavailable

logger = logging.getLogger(__name__)


class MemoryBackend(Protocol):
    """语义记忆读写协议。metadata 字段约定：{workspace_id, chat_id?, topic_id?, layer}。"""

    async def search(
        self,
        collection: str,
        query: str,
        *,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """返回 [{id, content, score, metadata, created_at?}]，按相关度降序。"""
        ...

    async def upsert(
        self,
        collection: str,
        content: str,
        *,
        metadata: dict[str, Any],
        memory_id: str | None = None,
    ) -> str:
        """写入或更新记忆，返回 memory_id；memory_id 给定即更新既有记忆（US8-11）。"""
        ...


class AgentRunMemoryBackend:
    """AgentRun 托管 collection 适配器。SDK 缺失或调用失败一律 MemoryUnavailable。"""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from agentrun.memory_collection import MemoryCollectionClient
        except ImportError as exc:
            raise MemoryUnavailable("agentrun-sdk not installed") from exc
        try:
            self._client = MemoryCollectionClient()
        except Exception as exc:  # 无平台凭据时构造即失败
            raise MemoryUnavailable(f"agentrun memory client unavailable: {exc}") from exc
        return self._client

    async def search(
        self,
        collection: str,
        query: str,
        *,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        try:
            client = self._get_client()
            return await client.search(collection, query, filter=filter, limit=limit)
        except MemoryUnavailable:
            raise
        except Exception as exc:
            raise MemoryUnavailable(f"memory search failed: {exc}") from exc

    async def upsert(
        self,
        collection: str,
        content: str,
        *,
        metadata: dict[str, Any],
        memory_id: str | None = None,
    ) -> str:
        try:
            client = self._get_client()
            return await client.upsert(collection, content, metadata=metadata, memory_id=memory_id)
        except MemoryUnavailable:
            raise
        except Exception as exc:
            raise MemoryUnavailable(f"memory upsert failed: {exc}") from exc
