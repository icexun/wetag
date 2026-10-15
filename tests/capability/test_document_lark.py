"""飞书文档适配器（``app/capability/integration/document/lark_cli.py``）。

全部离线：``FakeSpawn`` 注入进**真的** ``CliExecutor`` + 真的 ``LARK_COMMANDS``
登记表，回放 DocxXML 字节。于是被测的不只是解析逻辑，还有参数渲染、信封判定、
错误码归一这一整条链——而 ``lark-cli`` 的授权状态今日未验证（计划 R3，当日最大
单点）。真实调用一旦全面 ``AUTH_FAILED``，能顶住的只有这套离线断言。

这个文件守四条纪律，每条都对应一个**没有崩溃形态**的故障：

1. **``document_id`` 只认响应里的值。** 入参可能是 wiki token，而索引台账的
   ``UNIQUE (workspace_id, document_id)`` 落在真实 docx token 上。取错了不会报错，
   只会让同一篇文档经两条链接进来变成两行，同一处变更发两条通知。
2. **按章节读绝不退化成读全文。** ``SECTIONS`` 少了 anchor 是调用方的 bug，必须当场
   报错。悄悄读 WHOLE 只会表现为"这次比较慢、比较贵"，同时把整篇不可信正文塞进
   提示词，注入面从几段放大到全篇。
3. **两个命令各自只承认自己拿得到的字段。** ``fetch`` 没有 title/modified_time，
   ``metas`` 没有 revision_id。谁假装有，四元组里就会出现一个凭空的默认值，
   而新鲜度判定和章节 locator 全靠这四个字段。
4. **失败不得静默变成"读到了空文档"。** 一篇不可读要么抛 ``PortUnavailable``、
   要么被隔离在单篇，绝不能让 49 篇能读的文档陪着一起进不了索引——索引缺失的
   终点是变更通知静默停摆。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Sequence

import pytest

from app.capability.integration.document.lark_cli import (
    LarkDocumentAdapter,
    _normalize,
    _parse_fragment,
    _unix_to_dt,
)
from app.contracts.errors import PortUnavailable
from app.contracts.ids import DocumentId, IdemKey
from app.contracts.ports import (
    DocumentRef,
    DocumentUpdate,
    FetchScope,
    UpdateCommand,
    WikiSpaceRef,
)
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import LARK_COMMANDS, LarkDocsUpdateArgs

# 入参用的是 **wiki** token；真实 docx token 只在响应里。两者刻意不同。
WIKI_TOKEN = "WikiDemoToken0004"
WIKI_URL = f"https://example.feishu.cn/wiki/{WIKI_TOKEN}"
DOCX_TOKEN = "Zz1YdBBBBoLmxdxcTt7kAAAAAAA"

FRAGMENT = (
    "<fragment>"
    '<h1 id="blk1">额度中心</h1>'
    '<p id="blk2">单笔上限 5000 元，规则 A -&gt; B。</p>'
    '<h2 id="blk3">分期</h2>'
    '<p id="blk4">首行<br/>次行</p>'
    "</fragment>"
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeSpawn:
    """按 argv 回放。handler 可同步也可协程（并发上限用例要它能睡）。"""

    def __init__(self, handler: Callable[[list[str]], Any]) -> None:
        self._handler = handler
        self.calls: list[list[str]] = []
        self._inflight = 0
        self.peak = 0

    async def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: str | None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(list(argv))
        self._inflight += 1
        self.peak = max(self.peak, self._inflight)
        try:
            out = self._handler(list(argv))
            if asyncio.iscoroutine(out):
                out = await out
        finally:
            self._inflight -= 1
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, tuple):  # (exit_code, stdout, stderr)
            code, stdout, stderr = out
            return code, stdout.encode(), stderr.encode()
        return 0, json.dumps(out, ensure_ascii=False).encode(), b""

    # -- argv 查询 --------------------------------------------------------
    def calls_matching(self, *keys: str) -> list[list[str]]:
        return [c for c in self.calls if all(k in c for k in keys)]

    def argv_for(self, *keys: str) -> list[str]:
        got = self.calls_matching(*keys)
        if not got:
            raise AssertionError(f"没有调用过 {keys}；实际：{self.calls}")
        return got[0]


def flag(argv: Sequence[str], name: str) -> str | None:
    """取 ``--name`` 后面那个值。找不到返回 ``None``。"""
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def ok(document: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": {"document": document}}


def fail(kind: str, message: str) -> tuple[int, str, str]:
    """CLI 自报失败的信封。**权威失败信号只有它、退出码、stderr。**"""
    body = json.dumps(
        {"ok": False, "error": {"type": kind, "message": message}}, ensure_ascii=False
    )
    return 1, body, ""


def doc_payload(
    content: str = FRAGMENT, *, revision: int | None = 42, doc_id: str = DOCX_TOKEN
) -> dict[str, Any]:
    document: dict[str, Any] = {"document_id": doc_id, "content": content}
    if revision is not None:
        document["revision_id"] = revision
    return ok(document)


def adapter(
    handler: Callable[[list[str]], Any], **kw: Any
) -> tuple[LarkDocumentAdapter, FakeSpawn]:
    spawn = FakeSpawn(handler)
    executor = CliExecutor(
        workspace_id="alpha",
        binaries={"lark": "lark-cli"},
        secrets={"LARK_CLI_TOKEN": "s3cret-lark-token-value"},
        specs=LARK_COMMANDS,
        spawn=spawn,
        # 退避真睡会让重试用例花掉几秒钟；记下即返回。
        retry_sleep=lambda _d: asyncio.sleep(0),
    )
    return LarkDocumentAdapter(executor, **kw), spawn


def ref(*anchors: str) -> DocumentRef:
    """**入参一律给 wiki token/URL**——正是最容易把它误当 document_id 的形态。"""
    return DocumentRef(
        document_id=DocumentId(WIKI_TOKEN), url=WIKI_URL, anchors=tuple(anchors)
    )


# ---------------------------------------------------------------------------
# 一、document_id 的来源（索引唯一键的地基）
# ---------------------------------------------------------------------------


async def test_document_id_comes_from_the_response_never_from_the_wiki_token() -> None:
    """入参是 wiki token，快照里必须是响应给的 docx token。

    取错了没有任何报错：索引照样写入，只是同一篇文档经 wiki 链接和 docx 链接两条
    路进来会命中不同的 ``UNIQUE (workspace_id, document_id)``，于是库里两行。后果在
    推送时才显形——同一处变更推两条通知，而每一步看起来都完全正常。
    """
    a, spawn = adapter(lambda _argv: doc_payload())
    snap = await a.fetch(ref(), FetchScope.WHOLE)

    assert snap.meta.document_id == DOCX_TOKEN
    assert snap.meta.document_id != WIKI_TOKEN
    # 但传给 CLI 的仍是 wiki URL——解析 wiki→docx 是平台的活，不是我们猜的
    assert flag(spawn.argv_for("docs", "+fetch"), "--doc") == WIKI_URL


async def test_locator_is_built_from_the_real_document_id() -> None:
    """章节 locator 必须带真实 docx token + revision，否则点进去打不开。"""
    a, _ = adapter(lambda _argv: doc_payload(revision=77))
    snap = await a.fetch(ref(), FetchScope.WHOLE)
    assert snap.locator("blk2") == f"{DOCX_TOKEN}#blk2@rev77"


async def test_a_document_id_missing_from_the_response_yields_empty_not_the_input() -> None:
    """响应没给 document_id 时宁可留空，也不拿入参顶上。

    留空会让下游 locator 拼不出合法形态从而被丢弃；拿 wiki token 顶上则会拼出一个
    **看起来合法、实际指向错误对象**的 locator。前者少一条通知，后者给出一条
    可点开却打不开的链接。
    """
    a, _ = adapter(lambda _argv: ok({"content": FRAGMENT, "revision_id": 1}))
    snap = await a.fetch(ref(), FetchScope.WHOLE)
    assert snap.meta.document_id == ""


# ---------------------------------------------------------------------------
# 二、读取粒度：按章节读绝不退化成全文
# ---------------------------------------------------------------------------


async def test_sections_without_anchors_raises_instead_of_reading_the_whole_doc() -> None:
    """``SECTIONS`` 少 anchor 是调用方的 bug，必须当场炸。

    悄悄退化成 WHOLE 没有任何错误信号，只会表现为"这次慢一点、贵一点"，同时把整
    篇不可信正文塞进提示词——注入面从几段放大到全篇。所以还要断言**一次 CLI 都
    没发**：报错必须发生在花钱之前。
    """
    a, spawn = adapter(lambda _argv: doc_payload())
    with pytest.raises(ValueError, match="anchors"):
        await a.fetch(ref(), FetchScope.SECTIONS)
    assert spawn.calls == [], "参数错误却已经把命令发出去了"


async def test_sections_reads_one_request_per_anchor_and_never_asks_for_full() -> None:
    a, spawn = adapter(lambda _argv: doc_payload())
    await a.fetch(ref("blk1", "blk3"), FetchScope.SECTIONS)

    fetches = spawn.calls_matching("docs", "+fetch")
    assert len(fetches) == 2
    assert {flag(c, "--start-block-id") for c in fetches} == {"blk1", "blk3"}
    for c in fetches:
        assert flag(c, "--scope") == "section"
        assert flag(c, "--detail") == "with-ids"
    assert not [c for c in fetches if flag(c, "--scope") == "full"]


async def test_whole_and_outline_ask_for_the_scopes_they_advertise() -> None:
    """``OUTLINE`` 带深度上限；``WHOLE`` 才保留原文。

    ``raw_text`` 只在 WHOLE 下有值：其余粒度拿到的是残缺片段，留着它会让下游以为
    "整篇都在这儿"，于是拿一段去核对全篇的引文。
    """
    a, spawn = adapter(lambda _argv: doc_payload())

    whole = await a.fetch(ref(), FetchScope.WHOLE)
    assert flag(spawn.argv_for("docs", "+fetch"), "--scope") == "full"
    assert whole.raw_text == FRAGMENT

    spawn.calls.clear()
    out = await a.fetch(ref(), FetchScope.OUTLINE)
    argv = spawn.argv_for("docs", "+fetch")
    assert flag(argv, "--scope") == "outline"
    assert flag(argv, "--max-depth") == "3"
    assert out.raw_text is None
    assert out.outline == (("blk1", "额度中心"), ("blk3", "分期"))


async def test_outline_depth_is_injectable() -> None:
    a, spawn = adapter(lambda _argv: doc_payload(), outline_max_depth=5)
    await a.fetch(ref(), FetchScope.OUTLINE)
    assert flag(spawn.argv_for("docs", "+fetch"), "--max-depth") == "5"


async def test_concurrent_section_reads_are_capped() -> None:
    """章节并发有独立上限，**与 LLM 并发分开计量**。

    一篇文档二十个锚点就能把网关打到 ``RATE_LIMITED``，而限流的后果不是慢——是
    这一篇的章节读不回来，比对少掉章节。
    """

    async def slow(_argv: list[str]) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        return doc_payload()

    a, spawn = adapter(slow, fetch_concurrency=2)
    await a.fetch(ref(*[f"blk{i}" for i in range(8)]), FetchScope.SECTIONS)
    assert len(spawn.calls) == 8
    assert spawn.peak <= 2, f"并发上限没生效，峰值 {spawn.peak}"


# ---------------------------------------------------------------------------
# 三、部分失败：单篇隔离，不连坐
# ---------------------------------------------------------------------------


async def test_one_dead_anchor_does_not_sink_the_other_sections() -> None:
    """一个锚点 404，其余章节照常判定。

    全盘放弃会让一处笔误（引用了已删除的块）把整篇文档踢出比对，最终被当成
    "没有变化"——而文档里那处真变更还在。
    """

    def handler(argv: list[str]) -> Any:
        anchor = flag(argv, "--start-block-id") or ""
        if anchor == "blk3":
            return fail("not_found", "404 block not found")
        # ``scope=section`` 只回这一个锚点的子树，不是整篇——否则本用例断言不到
        # "少了谁"，因为每次请求都把两个章节一起带回来了。
        return doc_payload(
            content=f'<fragment><h1 id="{anchor}">只有这一节</h1><p id="p">正文</p></fragment>'
        )

    a, _ = adapter(handler)
    snap = await a.fetch(ref("blk1", "blk3"), FetchScope.SECTIONS)
    assert [s.anchor for s in snap.sections] == ["blk1"]
    assert snap.scope is FetchScope.SECTIONS


async def test_all_anchors_dead_raises_instead_of_an_empty_snapshot() -> None:
    """全部章节读不回来 ⇒ ``PortUnavailable``，**不是一份空快照**。

    空快照会被读成"这些章节没有变化"。这正是失败语义诚实要禁的那一步：
    读不到和读到了没问题，结论必须不同。
    """
    a, _ = adapter(lambda _argv: fail("internal_error", "503 service unavailable"))
    with pytest.raises(PortUnavailable):
        await a.fetch(ref("blk1", "blk3"), FetchScope.SECTIONS)


async def test_a_failed_whole_fetch_raises_rather_than_returning_nothing() -> None:
    """``unwrap()`` 这道闸门在这里落地：失败必须抛，不能返回空内容。"""
    a, _ = adapter(lambda _argv: fail("permission_denied", "403 forbidden"))
    with pytest.raises(PortUnavailable):
        await a.fetch(ref(), FetchScope.WHOLE)


@pytest.mark.parametrize(
    "payload, label",
    [
        ("not json at all", "非 JSON"),
        ({"ok": True, "data": {}}, "缺 document"),
        ({"ok": True, "data": {"document": {"document_id": "x"}}}, "缺 content"),
    ],
)
async def test_a_shape_change_in_the_response_is_an_error_not_an_empty_document(
    payload: Any, label: str
) -> None:
    """CLI 换版导致响应结构变化 ⇒ 报错，不是"这篇文档是空的"。

    ``content`` 键消失是升级 CLI 最常见的症状。把它读成空字符串，所有文档都会
    解析出零个章节，于是**每一次**比对都干净地报"没有变化"——一个全局静默失效。
    """
    a, _ = adapter(lambda _argv: (0, payload, "") if isinstance(payload, str) else payload)
    with pytest.raises(PortUnavailable):
        await a.fetch(ref(), FetchScope.WHOLE)


async def test_revision_takes_the_max_across_per_anchor_requests() -> None:
    """多次请求之间文档可能被改：revision 取最大值。

    取最小或取第一个会让 locator 指向一个**已经不含这段文字**的版本，人点进去
    看不到我们引用的内容，这条结论当场失去可信度。
    """
    revs = {"blk1": 10, "blk3": 31, "blk4": 22}

    def handler(argv: list[str]) -> Any:
        anchor = flag(argv, "--start-block-id") or ""
        return doc_payload(revision=revs[anchor])

    a, _ = adapter(handler)
    snap = await a.fetch(ref("blk1", "blk3", "blk4"), FetchScope.SECTIONS)
    assert snap.meta.revision_id == 31


async def test_sections_still_prefer_the_real_document_id_over_the_wiki_token() -> None:
    a, _ = adapter(lambda _argv: doc_payload())
    snap = await a.fetch(ref("blk1"), FetchScope.SECTIONS)
    assert snap.meta.document_id == DOCX_TOKEN


# ---------------------------------------------------------------------------
# 四、四元组：两个命令，各自只承认自己有的字段
# ---------------------------------------------------------------------------


async def test_fetch_does_not_invent_a_title_or_a_modified_time() -> None:
    """``fetch`` 拿不到 title/modified_time，就必须留空。

    这两项是 ``metas`` 的产出。假装有会得到一个**默认值**——而 ``modified_time``
    正是新鲜度判定的输入：一个凭空的 ``now()`` 会让每篇文档永远"刚刚更新"，
    30 分钟的 stale 闸门彻底失效，陈旧索引照样被当成没有变化。
    """
    a, _ = adapter(lambda _argv: doc_payload())
    snap = await a.fetch(ref(), FetchScope.WHOLE)
    assert snap.meta.title == ""
    assert snap.meta.modified_time is None
    # 但 fetch 独有的两项必须在
    assert snap.meta.revision_id == 42
    assert snap.meta.content_hash


async def test_batch_meta_has_title_and_mtime_but_admits_it_has_no_revision() -> None:
    """``metas`` 不返回 revision_id ⇒ 留 ``None``，由 fetch 补。

    随手填个 0 会让乐观锁写入带上一个必然过期的 base revision，
    ``docs +update`` 每次都 CONFLICT——表现是"写文档功能坏了"，查起来极难。
    """

    def handler(_argv: list[str]) -> Any:
        return {
            "ok": True,
            "data": {
                "metas": [
                    {
                        "doc_token": DOCX_TOKEN,
                        "title": "额度中心需求",
                        "latest_modify_time": "1758355200",
                        "owner_id": "ou_abc",
                    }
                ]
            },
        }

    a, _ = adapter(handler)
    metas = await a.batch_meta([DocumentRef(document_id=DocumentId(DOCX_TOKEN))])
    assert len(metas) == 1
    m = metas[0]
    assert m.title == "额度中心需求"
    assert m.modified_time is not None and m.modified_time.year == 2025
    assert m.revision_id is None, "metas 不返回 revision_id，不得凭空填"
    assert m.owner_id == "ou_abc"


def test_unix_seconds_arrive_as_a_string() -> None:
    """实测契约：``latest_modify_time`` 是 unix 秒的**字符串**。

    当成毫秒或当成 ISO 串解析都不会抛——只会得到一个 1970 年或 55000 年的时间，
    于是这篇文档要么永远陈旧、要么永远新鲜。
    """
    assert _unix_to_dt("1758355200") is not None
    assert _unix_to_dt(1758355200) is not None
    for junk in (None, "", 0, "0", "昨天", [1]):
        assert _unix_to_dt(junk) is None


# ---------------------------------------------------------------------------
# 五、batch_meta 的分批与降级（一篇无权限不得连坐 49 篇）
# ---------------------------------------------------------------------------


def _meta_tokens(argv: Sequence[str]) -> list[str]:
    body = json.loads(flag(argv, "--data") or "{}")
    return [d["doc_token"] for d in body.get("request_docs", [])]


async def test_batch_meta_splits_at_the_platform_limit() -> None:
    """单批 ≤50。超限的那一批平台整批拒，51 篇会一起查不到。"""
    seen: list[int] = []

    def handler(argv: list[str]) -> Any:
        tokens = _meta_tokens(argv)
        seen.append(len(tokens))
        return {
            "ok": True,
            "data": {"metas": [{"doc_token": t, "title": t} for t in tokens]},
        }

    a, _ = adapter(handler)
    refs = [DocumentRef(document_id=DocumentId(f"doc{i:03d}")) for i in range(51)]
    metas = await a.batch_meta(refs)

    assert seen == [50, 1]
    assert len(metas) == 51


async def test_one_unauthorized_doc_must_not_403_the_other_forty_nine() -> None:
    """整批失败 ⇒ 逐个重试。这是本文件最重要的一条。

    平台的行为是：一批里有一篇无权限，**整批** 403。若不降级到单条，49 篇能读的
    文档一起进不了索引；而索引缺失不会报错，只会让变更通知
    静默停摆。用户看到的是能力失效，日志里只有一行 403。
    """
    bad = "doc_no_permission"

    def handler(argv: list[str]) -> Any:
        tokens = _meta_tokens(argv)
        if bad in tokens and len(tokens) > 1:
            return fail("permission_denied", "403 forbidden: no access to one of docs")
        if tokens == [bad]:
            return fail("permission_denied", "403 forbidden")
        return {
            "ok": True,
            "data": {"metas": [{"doc_token": t, "title": t} for t in tokens]},
        }

    a, spawn = adapter(handler)
    refs = [DocumentRef(document_id=DocumentId(f"doc{i:03d}")) for i in range(49)]
    refs.append(DocumentRef(document_id=DocumentId(bad)))
    metas = await a.batch_meta(refs)

    got = {str(m.document_id) for m in metas}
    assert len(got) == 49
    assert bad not in got
    # 1 次整批 + 50 次单条。403 不可重试，所以正好一次一条。
    assert len(spawn.calls_matching("drive", "metas")) == 51


async def test_batch_meta_deduplicates_and_skips_refs_without_an_id() -> None:
    """同一 document_id 只查一次；没有 id 的 ref 直接跳过。

    重复查不只是浪费配额——``batch_meta`` 的结果会喂给索引刷新，重复的 meta 会让
    同一行被连写多次，掩盖 ``inserted`` 判定。
    """
    tokens_seen: list[list[str]] = []

    def handler(argv: list[str]) -> Any:
        tokens = _meta_tokens(argv)
        tokens_seen.append(tokens)
        return {
            "ok": True,
            "data": {"metas": [{"doc_token": t, "title": t} for t in tokens]},
        }

    a, _ = adapter(handler)
    await a.batch_meta(
        [
            DocumentRef(document_id=DocumentId("dup")),
            DocumentRef(document_id=DocumentId("dup"), url="别的链接"),
            DocumentRef(document_id=DocumentId("")),
        ]
    )
    assert tokens_seen == [["dup"]]


async def test_empty_refs_make_no_call_at_all() -> None:
    a, spawn = adapter(lambda _argv: pytest.fail("不该发出任何命令"))
    assert await a.batch_meta([]) == []
    assert spawn.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "data": {}},  # 没有 metas 键
        {"ok": True, "data": {"metas": "不是数组"}},
        {"ok": True, "data": {"metas": [{"title": "缺 doc_token"}]}},
    ],
)
async def test_a_malformed_meta_response_yields_no_fabricated_rows(
    payload: dict[str, Any]
) -> None:
    """结构不对时宁可少几行 meta，也不能造出 doc_token 为空的行。

    空 token 会成为索引的一行，并在下一次刷新时与真实行竞争同一个唯一键。
    """
    a, _ = adapter(lambda _argv: payload)
    metas = await a.batch_meta([DocumentRef(document_id=DocumentId("d1"))])
    assert all(str(m.document_id) for m in metas)


# ---------------------------------------------------------------------------
# 六、解析：引文要能逐字核对
# ---------------------------------------------------------------------------


def test_sections_carry_anchor_heading_and_level() -> None:
    sections, outline = _parse_fragment(FRAGMENT)
    assert [(s.anchor, s.heading, s.level) for s in sections] == [
        ("blk1", "额度中心", 1),
        ("blk3", "分期", 2),
    ]
    assert outline == [("blk1", "额度中心"), ("blk3", "分期")]


def test_entities_are_unescaped_because_quotes_are_compared_byte_for_byte() -> None:
    """``-&gt;`` 必须还原成 ``->``。

    章节摘录要与页面原文逐字一致。留着实体，页面上肉眼可见的 ``A -> B`` 在我们
    手里是 ``A -&gt; B``，引文与原文对不上——而且是静默对不上。
    """
    sections, _ = _parse_fragment(FRAGMENT)
    assert "A -> B" in sections[0].text
    assert "&gt;" not in sections[0].text


def test_line_structure_survives_normalization() -> None:
    """压空白但保留换行：``<br/>`` 还原成换行，引文按行比对。"""
    sections, _ = _parse_fragment(FRAGMENT)
    assert sections[1].text == "首行\n次行"
    # 连续空行压到两个，行内空白压到一个，但换行本身不动
    assert _normalize("a  \t b\n\n\n\nc") == "a b\n\nc"
    # 不换行空格（文档里到处都是）也要当空白处理，否则引文核对差一个字节
    assert _normalize("a  b") == "a b"


def test_a_headingless_document_becomes_one_anonymous_section() -> None:
    """纯段落文档整体作为一个匿名章节，anchor 取首个 block id。"""
    sections, outline = _parse_fragment(
        '<fragment><p id="only1">没有标题的正文</p><p id="only2">第二段</p></fragment>'
    )
    assert len(sections) == 1
    assert sections[0].anchor == "only1"
    assert sections[0].heading == ""
    assert "第二段" in sections[0].text
    assert outline == []


def test_no_block_id_means_no_section_because_there_could_be_no_locator() -> None:
    """一个 anchor 都没有 ⇒ 不产出章节。

    没有 anchor 就拼不出合法 locator。在这里就不给，比给一个无法定位的章节好——
    后者会产出一条"有变化但指不出在哪"的通知，而那种通知最难被发现是错的。
    """
    assert _parse_fragment("<fragment><p>没有 id 的段落</p></fragment>") == ([], [])


def test_an_empty_fragment_is_empty_not_a_crash() -> None:
    assert _parse_fragment("") == ([], [])
    assert _parse_fragment("<fragment></fragment>") == ([], [])


async def test_content_hash_tracks_content_not_metadata() -> None:
    """同内容同 hash、异内容异 hash——它是索引"要不要刷新"的唯一判据。"""
    a, _ = adapter(lambda _argv: doc_payload())
    first = await a.fetch(ref(), FetchScope.WHOLE)
    second = await a.fetch(ref(), FetchScope.WHOLE)
    assert first.meta.content_hash == second.meta.content_hash

    b, _ = adapter(lambda _argv: doc_payload(content=FRAGMENT + "<p id='x'>新增</p>"))
    third = await b.fetch(ref(), FetchScope.WHOLE)
    assert third.meta.content_hash != first.meta.content_hash


# ---------------------------------------------------------------------------
# 七、写入：乐观锁与"不盲重试"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command", [UpdateCommand.STR_REPLACE, UpdateCommand.BLOCK_REPLACE]
)
async def test_replacing_without_a_base_revision_is_refused(
    command: UpdateCommand,
) -> None:
    """替换类必须带 ``base_revision_id``，否则会覆盖别人刚写的内容。

    这是**丢数据**的形态，而且没有报错：写入成功、revision 前进，只是同事十分钟
    前补的那段没了。所以拦在发命令之前。
    """
    a, spawn = adapter(lambda _argv: pytest.fail("不该发出写入命令"))
    cmd = DocumentUpdate(
        ref=ref(), command=command, content="<p>新</p>", old_text="旧", anchor="blk2"
    )
    with pytest.raises(ValueError, match="base_revision_id"):
        await a.update(cmd, IdemKey("k1"))
    assert spawn.calls == []


async def test_append_needs_no_revision_and_carries_the_idem_key() -> None:
    """配对的正例：``append`` 不需要乐观锁，必须能照常写。

    少了这条，一个"永远抛 ValueError"的实现也能让上面那两个用例通过。
    """
    a, spawn = adapter(
        lambda _argv: {"ok": True, "data": {"document": {"revision_id": 99}}}
    )
    res = await a.update(
        DocumentUpdate(ref=ref(), command=UpdateCommand.APPEND, content="<p>追加</p>"),
        IdemKey("idem-1"),
    )
    assert res.ok is True
    assert res.revision_id == 99
    argv = spawn.argv_for("docs", "+update")
    assert flag(argv, "--command") == "append"
    assert flag(argv, "--doc") == WIKI_URL


async def test_the_base_revision_is_sent_as_the_optimistic_lock() -> None:
    a, spawn = adapter(
        lambda _argv: {"ok": True, "data": {"document": {"revision_id": 43}}}
    )
    await a.update(
        DocumentUpdate(
            ref=ref(),
            command=UpdateCommand.STR_REPLACE,
            content="<p>新</p>",
            old_text="单笔上限 5000 元",
            base_revision_id=42,
        ),
        IdemKey("idem-2"),
    )
    argv = spawn.argv_for("docs", "+update")
    assert flag(argv, "--revision-id") == "42"
    assert flag(argv, "--pattern") == "单笔上限 5000 元"


async def test_a_conflict_is_reported_and_never_blindly_retried() -> None:
    """乐观锁冲突 ⇒ ``conflict=True``，且**只发一次命令**。

    适配器自己重试要用旧 diff 打新版本，等于把别人刚写的内容直接覆盖掉。
    重算 diff 是 ``workflows/doc_update.py`` 的职责，不是这里的。
    """
    a, spawn = adapter(lambda _argv: fail("conflict", "409 revision mismatch"))
    res = await a.update(
        DocumentUpdate(
            ref=ref(),
            command=UpdateCommand.BLOCK_REPLACE,
            content="<p>新</p>",
            anchor="blk2",
            base_revision_id=42,
        ),
        IdemKey("idem-3"),
    )
    assert res.ok is False
    assert res.conflict is True
    assert res.error_code == "CONFLICT"
    assert len(spawn.calls_matching("docs", "+update")) == 1, "盲重试会覆盖他人修改"


async def test_a_non_conflict_write_failure_is_not_mislabelled_as_conflict() -> None:
    """403 不是冲突。混淆会让上层去"重算 diff 再试一次"，白烧一次配额，
    而真正该做的是发安全告警并停手。
    """
    a, _ = adapter(lambda _argv: fail("permission_denied", "403 forbidden"))
    res = await a.update(
        DocumentUpdate(ref=ref(), command=UpdateCommand.APPEND, content="<p>x</p>"),
        IdemKey("idem-4"),
    )
    assert res.ok is False
    assert res.conflict is False
    assert res.error_code == "PERMISSION_DENIED"


def test_the_write_path_cannot_express_a_deletion() -> None:
    """删除类**不可达**优于运行时拒绝。

    ``UpdateCommand`` 里没有删除成员，参数 model 的 Literal 也不收。于是"误删他人
    文档"这件事在类型层面就不存在，不依赖任何运行时判断记得拒绝。
    """
    assert not [c for c in UpdateCommand if "delete" in c.value]


# ---------------------------------------------------------------------------
# 八、全文评论
# ---------------------------------------------------------------------------


async def test_add_comment_posts_a_full_comment_with_one_text_element() -> None:
    """裸 docx token 必须显式 ``--type docx``；正文包成单条 text reply_element。

    ``--type`` 漏了，CLI 认不出裸 token 的文档类型，评论直接失败——而失败
    发生在通知发出之后，群里卡片已发、文档上却什么都没有。
    """
    a, spawn = adapter(lambda _argv: {"ok": True, "data": {"comment_id": "c1"}})
    await a.add_comment(
        DocumentRef(document_id=DocumentId(DOCX_TOKEN)),
        "冲突点：额度口径不一致",
        IdemKey("conflict_comment:f1"),
    )
    argv = spawn.argv_for("drive", "+add-comment")
    assert flag(argv, "--doc") == DOCX_TOKEN
    assert flag(argv, "--type") == "docx"
    assert "--full-comment" in argv
    elements = json.loads(flag(argv, "--content") or "[]")
    assert elements == [{"type": "text", "text": "冲突点：额度口径不一致"}]


async def test_add_comment_with_a_url_lets_the_cli_detect_the_type() -> None:
    """传 URL 时不带 ``--type``——wiki 链接由 CLI 自动识别，写死 docx 反而错。"""
    a, spawn = adapter(lambda _argv: {"ok": True, "data": {}})
    await a.add_comment(ref(), "x", IdemKey("k2"))
    argv = spawn.argv_for("drive", "+add-comment")
    assert flag(argv, "--doc") == WIKI_URL
    assert flag(argv, "--type") is None


async def test_a_failed_comment_raises_port_unavailable() -> None:
    """评论失败不得静默：调用方（router）靠异常决定降级，吞错等于假装已评论。"""
    a, _ = adapter(lambda _argv: fail("permission_denied", "403 forbidden"))
    with pytest.raises(PortUnavailable):
        await a.add_comment(
            DocumentRef(document_id=DocumentId(DOCX_TOKEN)), "x", IdemKey("k3")
        )
    with pytest.raises(Exception):
        LarkDocsUpdateArgs(doc="d", command="block_delete", content="")


# ---------------------------------------------------------------------------
# 八、知识库遍历
# ---------------------------------------------------------------------------


def _wiki_page(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ok": True, "data": {"items": items}}


def _node(
    token: str, obj_type: str, *, obj_token: str | None = None, child: bool = False
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "node_token": token,
        "obj_type": obj_type,
        "has_child": child,
    }
    if obj_token:
        node["obj_token"] = obj_token
    return node


async def test_list_tree_returns_docx_leaves_with_obj_token_as_the_document_id() -> None:
    """叶子的 document_id 是 ``obj_token``，不是 ``node_token``。

    又一处 wiki token ≠ docx token：拿 node_token 当 document_id，索引里整棵树的
    行都指向错误对象，之后每一次 fetch 都靠 URL 侥幸救回来，而唯一键早已错了。
    """

    def handler(argv: list[str]) -> Any:
        parent = flag(argv, "--parent-node-token")
        if parent is None:
            return _wiki_page(
                [
                    _node("wikiA", "docx", obj_token="docxA"),
                    _node("wikiFolder", "folder", child=True),
                    _node("wikiShort", "shortcut"),
                ]
            )
        if parent == "wikiFolder":
            return _wiki_page([_node("wikiB", "docx", obj_token="docxB")])
        return _wiki_page([])

    a, _ = adapter(handler)
    found = await a.list_tree(WikiSpaceRef(space_id="sp1"))

    assert [str(r.document_id) for r in found] == ["docxA", "docxB"]
    assert found[0].url == "https://example.feishu.cn/wiki/wikiA"


async def test_list_tree_respects_the_depth_limit() -> None:
    """深度上限是成本闸门：知识库可以有几千个节点。"""

    def handler(argv: list[str]) -> Any:
        parent = flag(argv, "--parent-node-token")
        if parent is None:
            return _wiki_page([_node("f1", "folder", child=True)])
        return _wiki_page([_node("deep", "docx", obj_token="docxDeep", child=True)])

    a, spawn = adapter(handler)
    assert await a.list_tree(WikiSpaceRef(space_id="sp1", max_depth=1)) == []
    assert len(spawn.calls_matching("wiki", "nodes")) == 1

    spawn.calls.clear()
    found = await a.list_tree(WikiSpaceRef(space_id="sp1", max_depth=2))
    assert [str(r.document_id) for r in found] == ["docxDeep"]


async def test_a_cycle_in_the_tree_does_not_loop_forever() -> None:
    """节点去重兼防环。知识库允许快捷方式指回祖先，没有 ``seen`` 就是死循环——
    表现是一次冷启动永不返回，而不是一个报错。
    """

    def handler(_argv: list[str]) -> Any:
        return _wiki_page([_node("same", "folder", child=True)])

    a, spawn = adapter(handler)
    assert await a.list_tree(WikiSpaceRef(space_id="sp1", max_depth=4)) == []
    assert len(spawn.calls_matching("wiki", "nodes")) <= 2


async def test_list_tree_handles_the_page_all_aggregate_shape() -> None:
    """``--page-all`` 把多页聚合成数组。两种形态都要认。"""

    def handler(argv: list[str]) -> Any:
        if flag(argv, "--parent-node-token") is not None:
            return _wiki_page([])
        return {
            "ok": True,
            "data": [
                {"items": [_node("w1", "docx", obj_token="d1")]},
                {"items": [_node("w2", "docx", obj_token="d2")]},
            ],
        }

    a, _ = adapter(handler)
    found = await a.list_tree(WikiSpaceRef(space_id="sp1"))
    assert [str(r.document_id) for r in found] == ["d1", "d2"]


async def test_a_failed_branch_does_not_abort_the_whole_walk() -> None:
    """一个子树无权限，其余子树照样收。

    整棵树放弃会让冷启动索引近乎为空——而空索引不报错，只让变更通知静默停摆。
    """

    def handler(argv: list[str]) -> Any:
        parent = flag(argv, "--parent-node-token")
        if parent is None:
            return _wiki_page(
                [
                    _node("okFolder", "folder", child=True),
                    _node("badFolder", "folder", child=True),
                ]
            )
        if parent == "badFolder":
            return fail("permission_denied", "403 forbidden")
        return _wiki_page([_node("w9", "docx", obj_token="d9")])

    a, _ = adapter(handler)
    found = await a.list_tree(WikiSpaceRef(space_id="sp1"))
    assert [str(r.document_id) for r in found] == ["d9"]
