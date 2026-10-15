"""变更通知：确定性比对与按订阅展开（``workflows/change_notify.py``）。"""

from __future__ import annotations

from typing import Any

from app.capability.workflows.change_notify import (
    Notifier,
    SectionDiff,
    diff_requirement,
    diff_sections,
    render_doc_change,
    render_requirement_change,
    section_digest,
)
from app.contracts.ids import DocumentId
from app.contracts.ports import (
    DocumentMeta,
    DocumentSection,
    DocumentSnapshot,
    FetchScope,
    Requirement,
    StatusCategory,
)


def _sec(anchor: str, heading: str, excerpt: str = "", hash_: str | None = None) -> dict:
    out: dict[str, Any] = {"anchor": anchor, "heading": heading, "excerpt": excerpt}
    if hash_ is not None:
        out["hash"] = hash_
    return out


def _req(**kw: Any) -> Requirement:
    base: dict[str, Any] = dict(
        requirement_id="",
        workspace_id="alpha",
        tapd_id="1001",
        title="分期",
        status="开发中",
        status_category=StatusCategory.ACTIVE,
        owners=("alice",),
        category="基础能力",
    )
    base.update(kw)
    return Requirement(**base)


# --- 文档 -------------------------------------------------------------------


def test_section_digest_stores_excerpt_and_a_stable_hash() -> None:
    snap = DocumentSnapshot(
        meta=DocumentMeta(document_id=DocumentId("d1"), title="t"),
        scope=FetchScope.WHOLE,
        sections=(
            DocumentSection(anchor="a1", heading="超时", text="x" * 500, level=2),
            DocumentSection(anchor="a2", heading="", text="   ", level=2),
        ),
    )

    first, again = section_digest(snap), section_digest(snap)

    assert len(first) == 1, "无标题且无正文的空章节不该入快照"
    assert len(first[0]["excerpt"]) == 200
    assert first[0]["hash"] == again[0]["hash"], "哈希不稳定，每轮都会误报修改"


def test_diff_sections_reports_added_removed_and_modified() -> None:
    old = [_sec("a1", "超时", hash_="h1"), _sec("a2", "背景", hash_="h2")]
    new = [_sec("a1", "超时", hash_="h1b"), _sec("a3", "范围", hash_="h3")]

    diff = diff_sections(old, new)

    assert diff == SectionDiff(added=("范围",), removed=("背景",), modified=("超时",))


def test_unchanged_sections_produce_an_empty_diff() -> None:
    secs = [_sec("a1", "超时", hash_="h1")]
    assert not diff_sections(secs, secs)


def test_a_renamed_heading_counts_as_modified() -> None:
    diff = diff_sections([_sec("a1", "超时", hash_="h")], [_sec("a1", "超时策略", hash_="h")])
    assert diff.modified == ("超时策略",)


def test_legacy_snapshot_without_hash_falls_back_to_excerpt() -> None:
    """旧快照没有 hash：退回摘录比较，而不是把每节都当成修改。"""
    old = [_sec("a1", "超时", "30 秒"), _sec("a2", "背景", "x")]
    new = [_sec("a1", "超时", "30 秒", "h1"), _sec("a2", "背景", "y", "h2")]

    assert diff_sections(old, new).modified == ("背景",)


def test_render_doc_change_links_the_doc_and_truncates_long_lists() -> None:
    names = tuple(f"第{i}节" for i in range(12))

    text = render_doc_change(
        title="支付策略", document_id="doc1", diff=SectionDiff(added=names), revision=7
    )

    assert text.startswith("**文档更新**：[支付策略](")
    assert "doc1" in text
    assert "等 12 节" in text and "第11节" not in text
    assert "版本：7" in text


def test_render_doc_change_with_no_structural_diff_still_says_something() -> None:
    text = render_doc_change(title="", document_id="doc1", diff=SectionDiff())
    assert "[doc1]" in text, "标题为空时用文档 ID 兜底"
    assert "正文有改动" in text


# --- 需求 -------------------------------------------------------------------


def test_diff_requirement_ignores_owner_order() -> None:
    prev = {"title": "分期", "status": "开发中", "owners": ["bob", "alice"], "category": "基础能力"}
    assert diff_requirement(prev, _req(owners=("alice", "bob"))) == []


def test_diff_requirement_lists_each_changed_field() -> None:
    prev = {"title": "分期", "status": "规划中", "owners": [], "category": None}

    changes = diff_requirement(prev, _req())

    assert [(c.field, c.old, c.new) for c in changes] == [
        ("status", "规划中", "开发中"),
        ("owners", "", "alice"),
        ("category", "", "基础能力"),
    ]


def test_render_requirement_change() -> None:
    changes = diff_requirement({"title": "分期", "status": "规划中", "owners": ["alice"],
                                "category": "基础能力"}, _req())

    text = render_requirement_change(_req(), changes, "https://tapd.example/1001")

    assert text == "**需求变更**：[分期](https://tapd.example/1001)（1001）\n- 状态：规划中 → 开发中"
    assert "[" not in render_requirement_change(_req(), changes, None).splitlines()[0]


# --- 推送 -------------------------------------------------------------------


class _Repo:
    def __init__(self, chats: list[str] | Exception) -> None:
        self._chats = chats

    async def subscribers(self, source: str, target: str) -> list[str]:
        if isinstance(self._chats, Exception):
            raise self._chats
        return list(self._chats)


async def test_notifier_fans_out_one_markdown_envelope_per_chat() -> None:
    n = Notifier(_Repo(["c1", "c2"]))

    envs = await n.envelopes("document", "doc1", "hi")

    assert [e.chat_id for e in envs] == ["c1", "c2"]
    assert all(e.payload == {"text": "hi"} for e in envs)
    assert all(e.merge_key is None for e in envs), "merge_key 会把通知并进 items，正文丢失"
    assert n.sent == 2


async def test_notifier_failure_does_not_break_the_sync() -> None:
    assert await Notifier(_Repo(ConnectionError("pg down"))).envelopes("document", "d", "x") == []
    assert await Notifier(None).envelopes("document", "d", "x") == []
