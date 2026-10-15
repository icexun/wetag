"""TAPD 同步 → 需求字段变更通知（``app/capability/workflows/tapd_watch.py``）。

核心断言：
- 首跑只建基线，零推送（冷启动不该向群里砸几十条通知）；
- 关注字段变化 → 推给订阅了该需求或全部需求的群；
- 只有 modified 变、关注字段没变 → 不推送；
- 没有群订阅 → 只落台账。
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.capability.workflows.tapd_watch import SEEN_KEY, make_tapd_sync_handler
from app.contracts.ids import DocumentId, WorkspaceId
from app.contracts.ports import DocumentRef, Requirement, StatusCategory, WorkspaceConfig


class FakeState:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.store[key] = value


class FakeConn:
    async def __aenter__(self) -> "FakeConn":
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


class FakeRepo:
    def __init__(self, subs: dict[str, list[str]] | None = None) -> None:
        self.db = SimpleNamespace(tx=lambda: FakeConn())
        self.rows: dict[str, dict[str, Any]] = {}
        self.subs = subs or {}
        self.upserted: list[dict[str, Any]] = []

    async def upsert_requirement(self, conn: Any, **kw: Any) -> Any:
        self.upserted.append(kw)
        self.rows[kw["tapd_id"]] = {
            "title": kw["title"],
            "status": kw["status"],
            "owners": list(kw["owners"]),
            "category": kw["category"],
        }
        return f"rid-{kw['tapd_id']}"

    async def requirements_by_tapd_ids(self, ids: list[str]) -> dict[str, Any]:
        return {i: dict(self.rows[i]) for i in ids if i in self.rows}

    async def subscribers(self, source: str, target: str) -> list[str]:
        assert source == "requirement"
        return sorted(set(self.subs.get(target, []) + self.subs.get("*", [])))


def _req(tapd_id: str, *, modified: str, status: str = "实现中", owners=("张三",)) -> Requirement:
    return Requirement(
        requirement_id="",
        tapd_id=tapd_id,
        workspace_id=WorkspaceId("alpha"),
        title=f"需求{tapd_id}",
        status=status,
        status_category=StatusCategory.ACTIVE,
        doc_ref=DocumentRef(document_id=DocumentId(f"doc{tapd_id}")),
        owners=tuple(owners),
        updated_at=datetime.fromisoformat(modified).replace(tzinfo=UTC),
    )


def _ctx(state: FakeState) -> Any:
    return SimpleNamespace(state=state, workspace_id="alpha")


def _container(repo: Any) -> Any:
    return SimpleNamespace(
        workspace=WorkspaceConfig(workspace_id=WorkspaceId("alpha"), tapd_project_id="1000001"),
        repository=repo,
    )


class FakePort:
    def __init__(self, reqs: list[Requirement]) -> None:
        self.reqs = reqs
        self.last_list_complete = True

    async def list_active(self, ws: Any) -> list[Requirement]:
        return self.reqs


async def _two_rounds(repo: FakeRepo, first: Requirement, second: Requirement):
    state = FakeState()
    handler = make_tapd_sync_handler(_container(repo))
    await handler(_ctx(state), FakePort([first]))
    return await handler(_ctx(state), FakePort([second]))


async def test_first_run_builds_baseline_without_notifying() -> None:
    state = FakeState()
    repo = FakeRepo(subs={"*": ["chat-a"]})
    handler = make_tapd_sync_handler(_container(repo))

    result = await handler(_ctx(state), FakePort([_req("1001", modified="2026-09-28T10:00:00")]))

    assert result.envelopes == []
    assert result.metrics["first_run"] is True
    assert repo.upserted[0]["doc_id"] == "doc1001"
    assert "1001" in state.store[SEEN_KEY]["seen"]


async def test_status_change_is_pushed_to_subscribed_chats() -> None:
    repo = FakeRepo(subs={"1001": ["chat-a"], "*": ["chat-b"]})
    result = await _two_rounds(
        repo,
        _req("1001", modified="2026-09-28T10:00:00"),
        _req("1001", modified="2026-09-28T11:00:00", status="已上线", owners=("李四",)),
    )

    assert sorted(e.chat_id for e in result.envelopes) == ["chat-a", "chat-b"]
    text = result.envelopes[0].payload["text"]
    assert "状态：实现中 → 已上线" in text
    assert "负责人：张三 → 李四" in text
    assert "https://www.tapd.cn/1000001/prong/stories/view/1001" in text
    assert result.metrics["notified"] == 2


async def test_modified_without_watched_field_change_is_silent() -> None:
    repo = FakeRepo(subs={"*": ["chat-a"]})
    result = await _two_rounds(
        repo,
        _req("1001", modified="2026-09-28T10:00:00"),
        _req("1001", modified="2026-09-28T11:00:00"),
    )
    assert result.envelopes == []
    assert result.metrics["changed"] == 1


async def test_no_subscriber_only_syncs() -> None:
    repo = FakeRepo()
    result = await _two_rounds(
        repo,
        _req("1001", modified="2026-09-28T10:00:00"),
        _req("1001", modified="2026-09-28T11:00:00", status="已上线"),
    )
    assert result.envelopes == []
    assert len(repo.upserted) == 2


async def test_without_repository_nothing_is_pushed() -> None:
    state = FakeState()
    handler = make_tapd_sync_handler(_container(None))
    await handler(_ctx(state), FakePort([_req("1001", modified="2026-09-28T10:00:00")]))
    result = await handler(
        _ctx(state), FakePort([_req("1001", modified="2026-09-28T11:00:00", status="已上线")])
    )
    assert result.envelopes == []
    assert result.metrics["synced"] == 0
