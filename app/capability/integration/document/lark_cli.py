"""飞书文档适配器（``lark-cli`` 实现 ``DocumentPort``）。

doc2 §5.3。三条设计约束，每条都对应一个已经踩过或必然会踩的坑：

1. **wiki token ≠ document_id**。传入 wiki 链接时，``docs +fetch`` 返回的
   ``data.document.document_id`` 才是真实 docx token。索引台账的
   ``UNIQUE (workspace_id, requirement_id, document_id)`` 必须落在这个真实 id
   上，否则同一篇文档经 wiki 链接和 docx 链接两条路进来会产生两行。
   因此 ``DocumentMeta.document_id`` **一律取响应里的值，不取入参**。

2. **四元组的字段来自两个不同命令**（实测契约）：

   ===================  ==========================================
   字段                 来源
   ===================  ==========================================
   ``document_id``      ``docs +fetch`` → ``data.document``
   ``revision_id``      ``docs +fetch``（``metas`` **不返回**它）
   ``modified_time``    ``drive metas batch_query``（unix 秒字符串）
   ``content_hash``     本地计算（平台不提供）
   ===================  ==========================================

   所以 ``fetch()`` 拿不到 title/modified_time，``batch_meta()`` 拿不到
   revision_id。两者互补，调用方按需组合，谁都不假装自己有全部字段。

3. **按章节读时绝不用 WHOLE**。``FetchScope.SECTIONS`` 按 anchor 逐个取，并发上限
   由构造函数注入（默认 3，与 LLM 并发分开计量）。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Sequence

from app.contracts.errors import PortUnavailable
from app.contracts.ids import DocumentId, IdemKey
from app.contracts.ports import (
    DocumentMeta,
    DocumentRef,
    DocumentSection,
    DocumentSnapshot,
    DocumentUpdate,
    FetchScope,
    UpdateCommand,
    WikiSpaceRef,
    WriteResult,
)
from app.capability.integration.document.urls import wiki_url
from app.platform.cli.executor import CliExecutor
from app.platform.cli.result import CliErrorCode
from app.platform.cli.registry import (
    LarkDocsFetchArgs,
    LarkDocsUpdateArgs,
    LarkDriveCommentAddArgs,
    LarkDriveMetasArgs,
    LarkWikiNodesArgs,
)

logger = logging.getLogger(__name__)

# 平台批量上限
_META_BATCH_SIZE = 50
# wiki 树里视为"叶子文档"的类型；其余（folder/shortcut/…）不进索引
_DOC_TYPES = frozenset({"docx", "doc"})
_HEADING_RE = re.compile(r"^h([1-9])$")


# ---------------------------------------------------------------------------
# DocxXML 解析
# ---------------------------------------------------------------------------


class _FragmentParser(HTMLParser):
    """把 ``<fragment>`` 拆成带 anchor 的章节。

    用 ``HTMLParser`` 而不是正则：DocxXML 的 block 可以嵌套（``<pre>`` 里有
    ``<code>``、列表里有段落），正则切不干净。这里只关心三件事——标题在哪、
    每个 block 的 id 是什么、纯文本是什么。

    ``<br/>`` 还原成换行；HTML 实体交给 ``convert_charrefs`` 自动反转义
    （``-&gt;`` → ``->``），因为证据核对要求引文与页面**逐字**一致。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.outline: list[tuple[str, str, int]] = []  # (anchor, heading, level)
        self._sections: list[dict[str, Any]] = []
        self._cur: dict[str, Any] | None = None
        self._pending_heading: dict[str, Any] | None = None
        self._first_id: str | None = None

    # -- 内部 -------------------------------------------------------------
    def _open_section(self, anchor: str, heading: str, level: int) -> None:
        self._flush()
        self._cur = {"anchor": anchor, "heading": heading, "level": level, "buf": []}

    def _flush(self) -> None:
        if self._cur is not None:
            self._sections.append(self._cur)
            self._cur = None

    # -- HTMLParser 回调 --------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        block_id = attr.get("id") or ""
        if block_id and self._first_id is None:
            self._first_id = block_id
        if tag == "br":
            if self._cur is not None:
                self._cur["buf"].append("\n")
            return
        m = _HEADING_RE.match(tag)
        if m and block_id:
            # 标题文本随后由 handle_data 送来，先挂起
            self._pending_heading = {"anchor": block_id, "level": int(m.group(1))}
            return
        if self._cur is not None and tag in {"p", "li", "pre", "tr", "div"}:
            self._cur["buf"].append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._pending_heading is not None:
            text = data.strip()
            if not text:
                return
            h = self._pending_heading
            self._pending_heading = None
            self.outline.append((h["anchor"], text, h["level"]))
            self._open_section(h["anchor"], text, h["level"])
            return
        if self._cur is not None:
            self._cur["buf"].append(data)

    # -- 结果 -------------------------------------------------------------
    def sections(self) -> list[DocumentSection]:
        self._flush()
        out: list[DocumentSection] = []
        for s in self._sections:
            text = _normalize("".join(s["buf"]))
            out.append(
                DocumentSection(
                    anchor=s["anchor"],
                    heading=s["heading"],
                    text=text,
                    level=s["level"],
                )
            )
        return out


_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")


def _normalize(text: str) -> str:
    """压空白但**保留换行结构**——引文核对要按行比对。"""
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL_RE.sub("\n\n", text).strip()


def _parse_fragment(content: str) -> tuple[list[DocumentSection], list[tuple[str, str]]]:
    p = _FragmentParser()
    p.feed(content)
    p.close()
    sections = p.sections()
    outline = [(a, h) for a, h, _lvl in p.outline]
    if not sections and content.strip():
        # 无标题的文档（纯段落）：整体作为一个匿名章节，anchor 取首个 block id。
        # 没有 anchor 就没有合法 locator，宁可不给证据（下游按 evidence_quality=0 丢弃）。
        anchor = p._first_id or ""
        text = _normalize(html.unescape(re.sub(r"<[^>]+>", "\n", content)))
        if anchor and text:
            sections = [DocumentSection(anchor=anchor, heading="", text=text, level=0)]
    return sections, outline


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _unix_to_dt(value: Any) -> datetime | None:
    """``latest_modify_time`` 实测是 unix 秒的**字符串**。"""
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        logger.warning("lark.meta 时间戳无法解析 value=%r", value)
        return None


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class LarkDocumentAdapter:
    """``DocumentPort`` 的飞书实现。

    所有外部调用都经 ``CliExecutor``：命令必须在白名单内、参数经 Pydantic
    校验、argv 数组传参、凭据按 workspace 注入、输出摘要脱敏后入审计。
    本类**不碰 subprocess，也不碰 stdout 原文**。
    """

    def __init__(
        self,
        executor: CliExecutor,
        *,
        fetch_concurrency: int = 3,
        outline_max_depth: int = 3,
    ) -> None:
        self._cli = executor
        self._sem = asyncio.Semaphore(max(1, fetch_concurrency))
        self._outline_max_depth = outline_max_depth

    # -- fetch ------------------------------------------------------------
    async def fetch(self, ref: DocumentRef, scope: FetchScope) -> DocumentSnapshot:
        if scope is FetchScope.SECTIONS:
            return await self._fetch_sections(ref)
        if scope is FetchScope.OUTLINE:
            args = LarkDocsFetchArgs(
                doc=ref.url or ref.document_id,
                scope="outline",
                max_depth=self._outline_max_depth,
                detail="with-ids",
            )
        else:
            args = LarkDocsFetchArgs(
                doc=ref.url or ref.document_id,
                scope="full",
                detail="with-ids",
            )
        payload = await self._fetch_one(args)
        return self._snapshot(payload, scope)

    async def _fetch_one(self, args: LarkDocsFetchArgs) -> dict[str, Any]:
        async with self._sem:
            result = await self._cli.run("lark.docs.fetch", args)
        if not result.ok:
            # unwrap() 抛 PortUnavailable：忘记判 ok 也不会静默退化成"没内容"
            result.unwrap()
        return self._document_of(result.data)

    @staticmethod
    def _document_of(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PortUnavailable(
                "lark.docs", CliErrorCode.OUTPUT_UNPARSABLE, "fetch 返回非 JSON 对象"
            )
        doc = (value.get("data") or {}).get("document")
        if not isinstance(doc, dict) or "content" not in doc:
            raise PortUnavailable(
                "lark.docs",
                CliErrorCode.OUTPUT_UNPARSABLE,
                "fetch 响应缺少 data.document.content",
            )
        return doc

    def _snapshot(self, doc: dict[str, Any], scope: FetchScope) -> DocumentSnapshot:
        content = doc.get("content") or ""
        sections, outline = _parse_fragment(content)
        meta = DocumentMeta(
            # 取响应里的真实 docx token，而非入参的 wiki token
            document_id=DocumentId(str(doc.get("document_id") or "")),
            title="",  # fetch 不返回 title，由 batch_meta 补
            revision_id=_as_int(doc.get("revision_id")),
            content_hash=_content_hash(content),
        )
        return DocumentSnapshot(
            meta=meta,
            scope=scope,
            sections=tuple(sections),
            outline=tuple(outline),
            raw_text=content if scope is FetchScope.WHOLE else None,
        )

    async def _fetch_sections(self, ref: DocumentRef) -> DocumentSnapshot:
        """按 anchor 逐个取章节。**只读部分章节时的唯一合法读法。**

        anchor 为空是调用方的 bug（必须先从 outline 选锚点），
        直接报错而不是悄悄退化成读全文——退化成 WHOLE 会把 token 成本和
        注入面一起放大。
        """
        if not ref.anchors:
            raise ValueError(
                "FetchScope.SECTIONS 必须提供 anchors；"
                "请先 fetch(OUTLINE) 选锚点，不要退化为 WHOLE"
            )

        async def one(anchor: str) -> tuple[str, dict[str, Any] | None]:
            args = LarkDocsFetchArgs(
                doc=ref.url or ref.document_id,
                scope="section",
                start_block_id=anchor,
                detail="with-ids",
                max_depth=2,
            )
            try:
                return anchor, await self._fetch_one(args)
            except PortUnavailable as exc:
                # 单个锚点失败不拖垮整篇：其余章节仍可判定，缺失记入 coverage_note
                logger.warning("lark 章节读取失败 anchor=%s err=%s", anchor, exc)
                return anchor, None

        results = await asyncio.gather(*(one(a) for a in ref.anchors))

        sections: list[DocumentSection] = []
        outline: list[tuple[str, str]] = []
        revision_id: int | None = None
        document_id = ref.document_id
        missing: list[str] = []
        hasher = hashlib.sha256()
        for anchor, doc in results:
            if doc is None:
                missing.append(anchor)
                continue
            content = doc.get("content") or ""
            hasher.update(content.encode("utf-8"))
            if doc.get("document_id"):
                document_id = DocumentId(str(doc["document_id"]))
            rev = _as_int(doc.get("revision_id"))
            if rev is not None:
                # 多次请求之间文档可能被改：取最大值，证据 locator 用它
                revision_id = rev if revision_id is None else max(revision_id, rev)
            secs, out = _parse_fragment(content)
            sections.extend(secs)
            outline.extend(out)

        if missing and not sections:
            raise PortUnavailable(
                "lark.docs",
                CliErrorCode.UNKNOWN,
                f"全部 {len(missing)} 个章节读取失败",
                retryable=True,
            )
        return DocumentSnapshot(
            meta=DocumentMeta(
                document_id=document_id,
                title="",
                revision_id=revision_id,
                content_hash=hasher.hexdigest(),
            ),
            scope=FetchScope.SECTIONS,
            sections=tuple(sections),
            outline=tuple(outline),
        )

    # -- batch_meta -------------------------------------------------------
    async def batch_meta(self, refs: Sequence[DocumentRef]) -> list[DocumentMeta]:
        """分批查元数据。单批 ≤50；**整批失败则逐个重试**。

        逐个重试不是为了好看：一篇无权限的文档会让整批 403，如果不降级到单条，
        49 篇能读的文档会一起进不了快照，它们的变更通知随之静默停摆。
        """
        uniq: dict[str, DocumentRef] = {}
        for r in refs:
            if r.document_id:
                uniq.setdefault(str(r.document_id), r)
        tokens = list(uniq)
        metas: list[DocumentMeta] = []
        for i in range(0, len(tokens), _META_BATCH_SIZE):
            chunk = tokens[i : i + _META_BATCH_SIZE]
            got = await self._metas(chunk)
            if got is None:
                logger.warning("metas 整批失败，降级逐个重试 size=%d", len(chunk))
                for token in chunk:
                    single = await self._metas([token])
                    if single:
                        metas.extend(single)
            else:
                metas.extend(got)
        return metas

    async def _metas(self, tokens: list[str]) -> list[DocumentMeta] | None:
        body = json.dumps(
            {
                "request_docs": [
                    {"doc_token": t, "doc_type": "docx"} for t in tokens
                ],
                "with_url": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._cli.run("lark.drive.metas", LarkDriveMetasArgs(data=body))
        if not result.ok:
            return None
        value = result.data
        if not isinstance(value, dict):
            return None
        data = value.get("data") or {}
        raw = data.get("metas")
        if not isinstance(raw, list):
            return None
        failed = data.get("failed_list") or []
        if failed:
            logger.warning("metas 部分文档不可访问 count=%d", len(failed))
        out: list[DocumentMeta] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            token = str(m.get("doc_token") or "")
            if not token:
                continue
            out.append(
                DocumentMeta(
                    document_id=DocumentId(token),
                    title=str(m.get("title") or ""),
                    # metas 不返回 revision_id——留 None，由 fetch 补
                    revision_id=None,
                    modified_time=_unix_to_dt(m.get("latest_modify_time")),
                    owner_id=str(m.get("owner_id") or "") or None,
                )
            )
        return out

    # -- list_meta（doc_poll 轮询形态）--------------------------------------
    async def list_meta(self, doc_ids: Sequence[str]) -> list[dict[str, Any]]:
        """``doc_poll`` 的版本比对视图：``[{"doc_id", "version"}]``。

        metas 接口不返回 revision_id，版本串只能用 ``latest_modify_time``——
        这正是设计（runtime.md：比对 latest_modify_time）。秒级精度对 3 分钟
        一轮的轮询足够；同一秒内改两次的情形由下一轮 content_hash 比对兜住。
        """
        refs = [DocumentRef(document_id=DocumentId(str(d))) for d in doc_ids if d]
        metas = await self.batch_meta(refs)
        return [
            {
                "doc_id": str(m.document_id),
                "version": m.modified_time.isoformat() if m.modified_time else "",
            }
            for m in metas
        ]

    # -- list_tree --------------------------------------------------------
    async def list_tree(self, space: WikiSpaceRef) -> list[DocumentRef]:
        """递归遍历知识库，返回 docx 叶子。深度上限 ``space.max_depth``。"""
        found: list[DocumentRef] = []
        seen: set[str] = set()

        async def walk(parent: str | None, depth: int) -> None:
            if depth > space.max_depth:
                return
            args = LarkWikiNodesArgs(
                space_id=space.space_id, parent_node_token=parent
            )
            result = await self._cli.run("lark.wiki.nodes", args)
            if not result.ok:
                logger.warning(
                    "wiki 节点遍历失败 space=%s parent=%s code=%s",
                    space.space_id,
                    parent,
                    result.error_code,
                )
                return
            for node in _wiki_items(result.data):
                token = str(node.get("node_token") or "")
                if not token or token in seen:
                    continue
                seen.add(token)
                obj_type = str(node.get("obj_type") or "")
                if obj_type in _DOC_TYPES:
                    obj_token = str(node.get("obj_token") or token)
                    found.append(
                        DocumentRef(
                            document_id=DocumentId(obj_token),
                            url=wiki_url(token),
                        )
                    )
                if node.get("has_child"):
                    await walk(token, depth + 1)

        await walk(space.root_node_token, 1)
        return found

    # -- update -----------------------------------------------------------
    async def update(self, cmd: DocumentUpdate, idem_key: IdemKey) -> WriteResult:
        """写文档。``base_revision_id`` 即乐观锁。

        返回 ``conflict=True`` 时**不在这里重试**：重试需要拿新版本重算 diff，
        那是 ``workflows/doc_update.py`` 的职责。适配器自己盲重试会把
        "基于旧版本的改写"直接覆盖掉别人刚写的内容。
        """
        if cmd.base_revision_id is None and cmd.command in (
            UpdateCommand.STR_REPLACE,
            UpdateCommand.BLOCK_REPLACE,
        ):
            raise ValueError(
                f"{cmd.command} 必须带 base_revision_id（乐观锁），否则会覆盖他人修改"
            )
        args = LarkDocsUpdateArgs(
            doc=cmd.ref.url or cmd.ref.document_id,
            command=cmd.command.value,
            content=cmd.content,
            revision_id=cmd.base_revision_id,
            pattern=cmd.old_text,
            block_id=cmd.anchor,
        )
        result = await self._cli.run("lark.docs.update", args, idem_key=idem_key)
        if result.ok:
            doc = (result.data or {}).get("data", {}).get("document", {})
            return WriteResult(ok=True, revision_id=_as_int(doc.get("revision_id")))
        conflict = result.error_code is CliErrorCode.CONFLICT
        return WriteResult(
            ok=False,
            conflict=conflict,
            error_code=result.error_code.value if result.error_code else None,
        )

    # -- add_comment ------------------------------------------------------
    async def add_comment(self, ref: DocumentRef, content: str, idem_key: IdemKey) -> None:
        """全文评论。

        纯文本包成单条 reply_element。失败抛 ``PortUnavailable``——评论是
        通知的增强，调用方（router）自己决定要不要降级，适配器不吞错。
        """
        elements = json.dumps([{"type": "text", "text": content}], ensure_ascii=False)
        args = LarkDriveCommentAddArgs(
            doc=ref.url or ref.document_id,
            content=elements,
            # 裸 token 必须显式声明类型；URL 由 CLI 自动识别
            type=None if ref.url else "docx",
        )
        result = await self._cli.run("lark.drive.comment_add", args, idem_key=idem_key)
        if not result.ok:
            result.unwrap()


def _wiki_items(value: Any) -> list[dict[str, Any]]:
    """兼容 ``--page-all`` 的聚合结构与单页结构。"""
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    if isinstance(data, list):  # --page-all 聚合成页数组
        out: list[dict[str, Any]] = []
        for page in data:
            if isinstance(page, dict):
                out.extend(i for i in (page.get("items") or []) if isinstance(i, dict))
        return out
    return []


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["LarkDocumentAdapter"]
