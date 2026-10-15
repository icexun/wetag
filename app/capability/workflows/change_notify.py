"""需求/文档变更通知：确定性比对 + 按订阅推送。

比对不调模型：同一处变化模型两次描述的措辞不会一致，"这次改了什么"必须可复现。
文档按章节比对（只存摘录与哈希，不落全文），需求按字段比对。

首次入库只建基线、不推送：没有上一版就没有"变更"，冷启动全量拉取不该刷屏。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.capability.integration.document.urls import docx_url
from app.contracts.envelope import Envelope, MsgType
from app.contracts.ports import DocumentSnapshot, Requirement

logger = logging.getLogger(__name__)

EXCERPT_CHARS = 200
_MAX_LISTED = 8


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


def section_digest(snapshot: DocumentSnapshot) -> list[dict[str, Any]]:
    """章节快照：摘录定位、哈希判变。"""
    return [
        {
            "anchor": s.anchor,
            "heading": s.heading,
            "excerpt": s.text[:EXCERPT_CHARS],
            "hash": hashlib.sha256(s.text.encode()).hexdigest()[:16],
        }
        for s in snapshot.sections
        if s.text.strip() or s.heading
    ]


@dataclass(frozen=True, slots=True)
class SectionDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.modified)


def _key(section: Mapping[str, Any]) -> str:
    return str(section.get("anchor") or section.get("heading") or "")


def _name(section: Mapping[str, Any]) -> str:
    return str(section.get("heading") or section.get("anchor") or "（无标题）")


def diff_sections(
    old: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]
) -> SectionDiff:
    """按 anchor 对齐。旧快照无 hash（迁移前数据）时退回摘录比较。"""
    before = {_key(s): s for s in old}
    after = {_key(s): s for s in new}
    modified = []
    for k, s in after.items():
        prev = before.get(k)
        if prev is None:
            continue
        if prev.get("hash") and s.get("hash"):
            changed = prev["hash"] != s["hash"]
        else:
            changed = prev.get("excerpt") != s.get("excerpt")
        if changed or prev.get("heading") != s.get("heading"):
            modified.append(_name(s))
    return SectionDiff(
        added=tuple(_name(s) for k, s in after.items() if k not in before),
        removed=tuple(_name(s) for k, s in before.items() if k not in after),
        modified=tuple(modified),
    )


def render_doc_change(
    *, title: str, document_id: str, diff: SectionDiff, revision: Any = None
) -> str:
    lines = [f"**文档更新**：[{title or document_id}]({docx_url(document_id)})"]
    for label, names in (("新增", diff.added), ("修改", diff.modified), ("删除", diff.removed)):
        if names:
            shown = "、".join(names[:_MAX_LISTED])
            more = f" 等 {len(names)} 节" if len(names) > _MAX_LISTED else ""
            lines.append(f"- {label}章节：{shown}{more}")
    if not diff:
        lines.append("- 正文有改动（章节结构未变）")
    if revision:
        lines.append(f"- 版本：{revision}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 需求
# ---------------------------------------------------------------------------

_FIELD_NAMES = {"title": "标题", "status": "状态", "owners": "负责人", "category": "分类"}


@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str
    old: str
    new: str


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value)
    return str(value)


def diff_requirement(prev: Mapping[str, Any], req: Requirement) -> list[FieldChange]:
    current = {
        "title": req.title,
        "status": req.status,
        "owners": sorted(req.owners),
        "category": req.category,
    }
    out = []
    for name, new in current.items():
        old = prev.get(name)
        if name == "owners":
            old = sorted(old or [])
        if _fmt(old) != _fmt(new):
            out.append(FieldChange(name, _fmt(old), _fmt(new)))
    return out


def render_requirement_change(
    req: Requirement, changes: Sequence[FieldChange], url: str | None = None
) -> str:
    head = f"[{req.title}]({url})" if url else req.title
    lines = [f"**需求变更**：{head}（{req.tapd_id}）"]
    for c in changes:
        lines.append(f"- {_FIELD_NAMES.get(c.field, c.field)}：{c.old or '空'} → {c.new or '空'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Notifier:
    """按订阅把一条变更展开成每群一个 Envelope。"""

    repository: Any
    sent: int = field(default=0, init=False)

    async def envelopes(self, source: str, target: str, text: str) -> list[Envelope]:
        if self.repository is None:
            return []
        try:
            chats = await self.repository.subscribers(source, target)
        except Exception as exc:  # noqa: BLE001 订阅表故障不阻断同步主流程
            logger.warning("订阅查询失败 %s:%s：%r", source, target, exc)
            return []
        self.sent += len(chats)
        return [
            Envelope(
                chat_id=chat_id,
                msg_type=MsgType.MARKDOWN,
                payload={"text": text},
            )
            for chat_id in chats
        ]


__all__ = [
    "FieldChange",
    "Notifier",
    "SectionDiff",
    "diff_requirement",
    "diff_sections",
    "render_doc_change",
    "render_requirement_change",
    "section_digest",
]
