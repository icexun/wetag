"""``RepoService``——``CodeRepositoryPort`` 的装配实现。

把 ``sync`` / ``search`` 两块拼成一个 Port。拆开是因为二者替换节奏不同：
``sync`` 的浅克隆策略基本定了，``search`` 可能从 ripgrep 换成索引服务。
"""

from __future__ import annotations

from datetime import datetime

from app.capability.integration.repo.search import RepoSearch
from app.capability.integration.repo.sync import RepoSync
from app.contracts.ports import (
    CodeEvidence,
    CodeQuery,
    RepoSnapshot,
    WorkspaceConfig,
)
from app.platform.cli.executor import CliExecutor


class RepoService:
    """``CodeRepositoryPort`` 实现。"""

    def __init__(
        self,
        executor: CliExecutor,
        *,
        repo_root: str,
        fetch_enabled: bool = True,
    ) -> None:
        self._repo_root = repo_root
        self._sync = RepoSync(executor, fetch_enabled=fetch_enabled)
        self._search = RepoSearch(executor)

    async def sync(self, ws: WorkspaceConfig) -> RepoSnapshot:
        return await self._sync.snapshot(ws)

    async def last_synced_at(self) -> datetime | None:
        """``repo_sync`` Job 的新鲜度来源（US3-11）：本仓最近一次 fetch 时刻。"""
        return await self._sync.last_synced_at(self._repo_root)

    async def search(self, q: CodeQuery) -> list[CodeEvidence]:
        return await self._search.search(q)


__all__ = ["RepoService"]
