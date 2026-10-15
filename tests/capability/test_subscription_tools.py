"""Agent 订阅工具：群归属取自任务绑定，模型不能替别的群订阅。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.capability.agents import subscription_tools as st
from app.capability.agents.wecom_tools import bind_task_context, unbind_task_context


class _Repo:
    def __init__(self) -> None:
        self.rows: set[tuple[str, str, str]] = set()
        self.created_by: list[Any] = []

    async def subscribe(self, *, chat_id: str, source: str, target: str, created_by: Any) -> bool:
        self.created_by.append(created_by)
        key = (chat_id, source, target)
        new = key not in self.rows
        self.rows.add(key)
        return new

    async def unsubscribe(self, *, chat_id: str, source: str, target: str) -> int:
        key = (chat_id, source, target)
        if key in self.rows:
            self.rows.remove(key)
            return 1
        return 0

    async def list_subscriptions(self, chat_id: str) -> list[dict]:
        return [{"source": s, "target": t} for c, s, t in sorted(self.rows) if c == chat_id]


def _out(resp: Any) -> dict:
    return json.loads(resp.content[0]["text"])


@pytest.fixture()
def repo():
    r = _Repo()
    st.bind_dependencies(r)
    yield r
    st.bind_dependencies(None)


@pytest.fixture()
def in_chat():
    token = bind_task_context(task_id="t1", actor_user_id="u1", chat_id="chat-a", topic_id=None)
    yield
    unbind_task_context(token)


async def test_subscribe_list_unsubscribe_roundtrip(repo: _Repo, in_chat: None) -> None:
    first = _out(await st.wetag_subscribe_changes("document", "doc1"))
    again = _out(await st.wetag_subscribe_changes("document", "doc1"))

    assert first["ok"] and first["created"] is True
    assert again["created"] is False
    assert repo.created_by == ["u1", "u1"]
    listed = _out(await st.wetag_list_subscriptions())
    assert listed["subscriptions"] == [{"source": "document", "target": "doc1"}]

    assert _out(await st.wetag_unsubscribe_changes("document", "doc1"))["removed"] == 1
    assert _out(await st.wetag_list_subscriptions())["subscriptions"] == []


async def test_blank_target_means_all(repo: _Repo, in_chat: None) -> None:
    await st.wetag_subscribe_changes("requirement", "  ")
    assert ("chat-a", "requirement", "*") in repo.rows


async def test_unknown_source_is_refused(repo: _Repo, in_chat: None) -> None:
    out = _out(await st.wetag_subscribe_changes("tag", "*"))
    assert out["ok"] is False and "requirement" in out["error"]
    assert repo.rows == set()


async def test_without_chat_binding_nothing_is_written(repo: _Repo) -> None:
    out = _out(await st.wetag_subscribe_changes("document", "doc1"))
    assert out["ok"] is False
    assert repo.rows == set()


async def test_without_pg_the_tool_says_so(in_chat: None) -> None:
    st.bind_dependencies(None)
    out = _out(await st.wetag_subscribe_changes("document", "doc1"))
    assert out["ok"] is False and "PG" in out["error"]
