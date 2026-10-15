"""tests/control 共享 fixture：连接池、Rules 仓储、告警收集。"""
from __future__ import annotations

import pytest

from app.control.approval.pending import PendingApprovalRepository
from app.control.repository import RulesRepository
from app.platform.db.pool import create_pool
from app.platform.observability.alerting import clear_sinks, register_sink
from app.runtime.repository import TaskRepository
from app.runtime.state import RuntimeStateRepository


@pytest.fixture()
async def pool(pg_schema):
    p = await create_pool(pg_schema.admin_dsn, pg_schema.workspace, min_size=1, max_size=4)
    yield p
    await p.close()


@pytest.fixture()
def rules_repo(pool):
    return RulesRepository(pool)


@pytest.fixture()
def task_repo(pool):
    return TaskRepository(pool)


@pytest.fixture()
def approval_repo(pool):
    return PendingApprovalRepository(pool)


@pytest.fixture()
def state_store(pool):
    return RuntimeStateRepository(pool)


@pytest.fixture()
def alerts():
    """内存告警 sink：收集本测试内全部告警。"""
    items: list = []
    register_sink(items.append)
    yield items
    clear_sinks()


class FakeMemoryBackend:
    """语义记忆替身：collection → 记忆行的内存存储。

    - search：token 重叠打分（相同内容得分 1.0），支持 metadata 等值过滤；
    - upsert：memory_id 给定即更新既有行，否则新增；
    - fail=True 时一切调用抛 MemoryUnavailable，模拟服务不可用（US8-9）。
    """

    def __init__(self) -> None:
        self.store: dict[str, list[dict]] = {}
        self.fail = False
        self.search_calls: list[dict] = []
        self._seq = 0

    def seed(
        self,
        collection: str,
        content: str,
        *,
        metadata: dict | None = None,
        score_id: str | None = None,
    ) -> str:
        self._seq += 1
        memory_id = score_id or f"mem-{self._seq}"
        self.store.setdefault(collection, []).append(
            {"id": memory_id, "content": content, "metadata": dict(metadata or {})}
        )
        return memory_id

    def all_records(self, collection: str) -> list[dict]:
        return list(self.store.get(collection, []))

    @staticmethod
    def _score(query: str, content: str) -> float:
        q, c = set(query.split()), set(content.split())
        if not q:
            return 0.0
        return len(q & c) / len(q)

    @staticmethod
    def _match(metadata: dict, filter: dict | None) -> bool:
        if not filter:
            return True
        return all(metadata.get(k) == v for k, v in filter.items())

    async def search(self, collection, query, *, filter=None, limit=10):
        from app.contracts.errors import MemoryUnavailable

        if self.fail:
            raise MemoryUnavailable("fake backend down")
        self.search_calls.append({"collection": collection, "query": query, "filter": filter})
        hits = []
        for rec in self.store.get(collection, []):
            if not self._match(rec["metadata"], filter):
                continue
            score = self._score(query, rec["content"])
            if score > 0:
                hits.append({**rec, "score": score})
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:limit]

    async def upsert(self, collection, content, *, metadata, memory_id=None):
        from app.contracts.errors import MemoryUnavailable

        if self.fail:
            raise MemoryUnavailable("fake backend down")
        rows = self.store.setdefault(collection, [])
        if memory_id is not None:
            for rec in rows:
                if rec["id"] == memory_id:
                    rec["content"] = content
                    rec["metadata"] = dict(metadata)
                    return memory_id
        self._seq += 1
        new_id = memory_id or f"mem-{self._seq}"
        rows.append({"id": new_id, "content": content, "metadata": dict(metadata)})
        return new_id


@pytest.fixture()
def memory_backend() -> FakeMemoryBackend:
    return FakeMemoryBackend()
