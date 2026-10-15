"""AgentRun 托管 collection 的三层解析（plan Decision 5）。

- 业务线级 = 进程级 collection（`MEMORY_COLLECTION_NAME`），隔离由「进程只有
  自己业务线的 collection 名与凭据」免费获得（US8-2）。
- 组织级 = `ORG_MEMORY_COLLECTION_NAME`，只读；写入只能走 pending_memory 提案（US8-6）。
- 群级 = 业务线 collection 内按 metadata `chat_id` 过滤。

collection 名缺失（本地无平台凭据的常态）即记忆服务不可用：
`resolve()` 抛 `MemoryUnavailable`，由召回侧转为显式降级（US8-9）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.contracts.errors import MemoryUnavailable
from config import get

# 组织级只读 collection 的配置键（联调时与平台侧校准命名，explore.md 待确认 #2）
ORG_MEMORY_COLLECTION_NAME = "ORG_MEMORY_COLLECTION_NAME"
BIZ_MEMORY_COLLECTION_NAME = "MEMORY_COLLECTION_NAME"


@dataclass(frozen=True)
class MemoryLayers:
    """一次召回/写入涉及的三层 collection 解析结果。"""

    workspace_id: str
    biz_collection: str
    org_collection: str | None = None

    def group_filter(self, chat_id: str) -> dict[str, str]:
        """群级 = 业务线 collection 内按 metadata 过滤（US8-1 来源标注字段）。"""
        return {"workspace_id": self.workspace_id, "chat_id": chat_id, "layer": "group"}

    def biz_filter(self) -> dict[str, str]:
        """业务线层不加 chat_id 过滤——同业务线跨群决策可召回（US8-3）。"""
        return {"workspace_id": self.workspace_id, "layer": "biz"}


class MemoryCollections:
    """三层 collection 名解析。名为空即服务不可用，显式抛错而非静默空结果。"""

    def __init__(
        self,
        workspace_id: str,
        *,
        biz_collection: str | None = None,
        org_collection: str | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        # 显式注入优先（测试 / 联调），缺省走配置链（env → config-service）
        self._biz_collection = (
            biz_collection if biz_collection is not None else get(BIZ_MEMORY_COLLECTION_NAME)
        )
        self._org_collection = (
            org_collection if org_collection is not None else get(ORG_MEMORY_COLLECTION_NAME)
        )

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def resolve(self) -> MemoryLayers:
        """解析三层 collection；业务线 collection 名缺失即不可用（US8-9）。"""
        if not self._biz_collection:
            raise MemoryUnavailable(
                f"memory collection not configured for workspace {self._workspace_id}"
            )
        return MemoryLayers(
            workspace_id=self._workspace_id,
            biz_collection=self._biz_collection,
            org_collection=self._org_collection or None,
        )
