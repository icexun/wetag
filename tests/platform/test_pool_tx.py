"""短事务纪律 + 三层隔离护栏（§9.4-2/3、doc2 §A.10-1）。

**一个 Workflow step = 一个 tx；tx 块内禁止 ``await`` LLM / CLI / HTTP。**

这条纪律靠三重护栏落地，本文件逐条验：

1. 运行期软阈值 WARN 带调用栈（``Database.tx``）——事后能查；
2. 连接级 ``idle_in_transaction_session_timeout``——最后的兜底自杀；
3. **本文件的用例**——事前就红。

前两层都是"出事之后"。真正能阻止违规进主干的只有第三层：一个记录 tx 进出与外部
调用时序的 ``RecordingDatabase``，任何"在 tx 里 await 一次远程调用"的写法都会让
时序穿插，用例当场看得见。

写这个文件时就抓到一处真违规：``doc_update`` 把 ``await _embed(...)`` 写在
``refresh_index`` 的实参里，而那整个调用在 ``async with tx()`` 之内——一次远程
向量调用（几百毫秒到几秒）全程持有一条连接和一个打开的事务。100 条需求每 3 分钟
一轮，足以耗干 10 连接的池，而症状会表现为"查询偶发超时"，离真正的原因隔了两层。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest

from app.contracts.errors import IsolationViolation, PoolExhausted, PortUnavailable
from app.platform.db.pool import Database
from app.platform.db.repository import Repository
from app.platform.db.schema import (
    render_migration,
    safe_identifier,
    search_path,
    ws_role,
    ws_schema,
)


# --- 时序记录型 Fake --------------------------------------------------------


class RecordingDatabase:
    """记录 tx 进出与库内操作的时序。

    刻意只记**顺序**而不记内容：纪律说的是"tx 块内不许有外部调用"，那是一个时序
    性质，和具体 SQL 无关。
    """

    def __init__(self) -> None:
        self.timeline: list[str] = []
        self.depth = 0
        self.max_depth = 0

    @asynccontextmanager
    async def tx(self) -> AsyncIterator["RecordingDatabase"]:
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.timeline.append("tx.begin")
        try:
            yield self
        finally:
            self.depth -= 1
            self.timeline.append("tx.commit")

    def note(self, what: str) -> None:
        """外部调用打点。在 tx 内被调用时会在时序里夹在 begin/commit 之间。"""
        self.timeline.append(what)

    @property
    def tx_count(self) -> int:
        return self.timeline.count("tx.begin")

    def awaits_inside_tx(self) -> list[str]:
        """返回所有出现在 tx 块内的外部调用打点。"""
        inside: list[str] = []
        depth = 0
        for ev in self.timeline:
            if ev == "tx.begin":
                depth += 1
            elif ev == "tx.commit":
                depth -= 1
            elif depth > 0:
                inside.append(ev)
        return inside


# --- 纪律：外部调用不得在 tx 内 --------------------------------------------


async def test_the_recorder_itself_can_see_a_violation() -> None:
    """先证明这把尺子有刻度。

    如果 ``awaits_inside_tx`` 恒返回空列表，下面每一条断言都是空的。所以先写一段
    **故意违规**的代码，确认它被抓到——这是断言工具本身的自测。
    """
    db = RecordingDatabase()

    async with db.tx():
        db.note("llm.complete")  # 违规：远程调用在事务里

    assert db.awaits_inside_tx() == ["llm.complete"]


async def test_the_compliant_shape_records_nothing_inside() -> None:
    """合规写法：先算完，再开事务写库。"""
    db = RecordingDatabase()

    db.note("llm.complete")  # 事务之外
    async with db.tx():
        pass

    assert db.awaits_inside_tx() == []
    assert db.tx_count == 1


async def test_doc_update_computes_the_embedding_outside_the_transaction() -> None:
    """``refresh_index_job``：向量必须在 tx 之外算完。

    这正是本文件抓到的那处真违规的回归守卫。``embedder.embed`` 是远程调用；写在
    ``refresh_index(...)`` 的实参里看起来只是"顺手取个值"，但求值发生在 tx 块内。

    这里不 mock 数据库层的 SQL，只看时序：embed 的打点必须落在 tx 之外。
    """
    from app.capability.workflows import doc_update as du
    from app.contracts.ports import (
        DocumentMeta,
        DocumentSection,
        DocumentSnapshot,
        FetchScope,
    )

    db = RecordingDatabase()

    def _snapshot() -> DocumentSnapshot:
        return DocumentSnapshot(
            meta=DocumentMeta(
                document_id="doc1",  # type: ignore[arg-type]
                title="提现费率",
                revision_id=42,
                content_hash="h1",
            ),
            scope=FetchScope.WHOLE,
            sections=(DocumentSection(anchor="a1", heading="费率", text="单笔 2 元"),),
            outline=(("a1", "费率"),),
        )

    class FakeEmbedder:
        async def embed(self, text: str) -> list[float]:
            db.note("embedder.embed")
            await asyncio.sleep(0)  # 让出事件循环，模拟真实的网络往返
            return [0.1, 0.2]

    class FakeRepo:
        def __init__(self) -> None:
            self.db = db

        async def get_index(self, document_id: Any) -> Any:
            return None  # 不打点——它在 tx 之外是合法的

        async def refresh_index(self, conn: Any, **kw: Any) -> Any:
            from app.capability.repository import IndexRefresh

            db.note("sql.refresh_index")
            assert kw["embedding"] == [0.1, 0.2], "向量要真的被带进来，而不是丢成 None"
            return IndexRefresh.INSERTED

        async def touch_index(self, conn: Any, **kw: Any) -> None:
            db.note("sql.touch_index")

    class FakeDocs:
        async def fetch(self, ref: Any, scope: Any) -> Any:
            db.note("documents.fetch")
            return _snapshot()

    await du.refresh_index(
        document_ids=["d1", "d2"],
        documents=FakeDocs(),
        repository=FakeRepo(),  # type: ignore[arg-type]
        embedder=FakeEmbedder(),
    )

    inside = db.awaits_inside_tx()
    assert [e for e in inside if not e.startswith("sql.")] == [], (
        f"tx 块内出现了外部调用：{inside}"
    )
    # 每篇文档各自一个短事务，不是把整轮刷新包进一个大事务
    assert db.tx_count == 2
    assert db.max_depth == 1, "事务不得嵌套——嵌套等于把两个 step 绑成一个"
    # 顺序上，远程调用都在各自那个 tx 之前
    assert db.timeline.index("embedder.embed") < db.timeline.index("tx.begin")


async def test_idempotency_awaits_the_external_action_between_transactions() -> None:
    """``Idempotency.guard``：外部写必须夹在两个事务**之间**。

    这是整条纪律最关键的一处：抢占（tx1）、真正的外部调用（无事务）、记账（tx2）。
    把外部调用放进 tx1 会让一次 TAPD 建卡（可能几秒）全程持有事务；而如果反过来
    省掉 tx2，崩溃后这条记录就永远停在 ``in_flight``。
    """
    from app.capability.integration.idempotency import Idempotency

    db = RecordingDatabase()

    class FakeRepo:
        async def claim(self, conn: Any, *, key: str, operation: str) -> tuple[bool, Any]:
            db.note("sql.claim")
            return True, {"attempt": 1}

        async def settle(self, conn: Any, **kw: Any) -> None:
            db.note(f"sql.settle:{kw.get('status')}")

    async def action() -> str:
        db.note("tapd.create_story")  # 外部写
        await asyncio.sleep(0)
        return "story-1"

    idem = Idempotency(db, FakeRepo())  # type: ignore[arg-type]
    out = await idem.guard("k1", "tapd.create", action)  # type: ignore[arg-type]

    assert out.value == "story-1"
    assert db.awaits_inside_tx() == ["sql.claim", "sql.settle:succeeded"]
    assert "tapd.create_story" not in db.awaits_inside_tx()
    assert db.tx_count == 2  # 抢占 + 记账，各自独立
    assert db.timeline == [
        "tx.begin", "sql.claim", "tx.commit",
        "tapd.create_story",
        "tx.begin", "sql.settle:succeeded", "tx.commit",
    ]


async def test_idempotency_failure_path_settles_in_its_own_transaction() -> None:
    """失败路径也要落库，且用**独立**事务。

    复用抢占那个事务是不可能的（它早已提交），而异常路径上更不该假设连接还健康。
    如果失败不落库，``attempt`` 不递增，重试次数就无从限制。
    """
    from app.capability.integration.idempotency import Idempotency

    db = RecordingDatabase()

    class FakeRepo:
        async def claim(self, conn: Any, *, key: str, operation: str) -> tuple[bool, Any]:
            db.note("sql.claim")
            return True, {"attempt": 1}

        async def settle(self, conn: Any, **kw: Any) -> None:
            db.note(f"sql.settle:{kw.get('status')}:{kw.get('error_code')}")

    async def action() -> str:
        db.note("tapd.create_story")
        raise TimeoutError("上游超时")

    idem = Idempotency(db, FakeRepo())  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        await idem.guard("k1", "tapd.create", action)  # type: ignore[arg-type]

    assert db.tx_count == 2
    assert "sql.settle:failed:TimeoutError" in db.timeline
    assert "tapd.create_story" not in db.awaits_inside_tx()


# --- Database.tx：软阈值与池耗尽 --------------------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.executed.append("BEGIN")
        yield
        self.executed.append("COMMIT")

    async def execute(self, sql: str, *a: Any) -> str:
        self.executed.append(sql)
        return "OK"


class _FakePool:
    """最小 asyncpg.Pool 替身。只有 Database 真正用到的那几个方法。"""

    def __init__(self, *, acquire_delay: float = 0.0) -> None:
        self.conn = _FakeConn()
        self._delay = acquire_delay
        self.released = 0

    async def acquire(self) -> _FakeConn:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self.conn

    async def release(self, conn: _FakeConn) -> None:
        self.released += 1

    def get_size(self) -> int:
        return 10

    def get_idle_size(self) -> int:
        return 0

    async def close(self) -> None:
        pass


def _db(pool: _FakePool, **kw: Any) -> Database:
    db = Database("postgresql://x/y", "alpha", **kw)
    db._pool = pool  # type: ignore[assignment]
    return db


async def test_slow_transaction_warns_with_a_stack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """超过软阈值的事务要打 WARN，**并带调用栈**。

    没有栈的告警在这里几乎没用：日志里只说"某处有个 3 秒的事务"，而违规点可能在
    十几个 Workflow step 里的任意一个。栈是唯一能直接指到那一行的东西。
    """
    pool = _FakePool()
    db = _db(pool, tx_soft_limit_ms=1)

    with caplog.at_level(logging.WARNING, logger="app.platform.db.pool"):
        async with db.tx():
            await asyncio.sleep(0.02)

    warns = [r for r in caplog.records if "long transaction" in r.getMessage()]
    assert len(warns) == 1
    msg = warns[0].getMessage()
    assert "workspace_id=alpha" in msg
    assert "test_slow_transaction_warns_with_a_stack" in msg, "告警里必须能看到调用栈"


async def test_fast_transaction_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """正常短事务不要刷 WARN。

    与上一条成对。一个恒告警的实现会让这条 WARN 在几天内被所有人过滤掉，
    真正的长事务就再也没人看见了——告警的价值来自它的稀有。
    """
    pool = _FakePool()
    db = _db(pool, tx_soft_limit_ms=2_000)

    with caplog.at_level(logging.WARNING, logger="app.platform.db.pool"):
        async with db.tx():
            pass

    assert [r for r in caplog.records if "long transaction" in r.getMessage()] == []


async def test_transaction_releases_the_connection_even_on_error() -> None:
    """事务里抛异常也要归还连接。

    漏还一条就少一条，池会被慢慢"吃掉"——而这类泄漏只在高峰期表现为
    ``PoolExhausted``，那时离真正的原因已经隔了几个小时。
    """
    pool = _FakePool()
    db = _db(pool)

    with pytest.raises(ValueError):
        async with db.tx():
            raise ValueError("boom")

    assert pool.released == 1


async def test_acquire_timeout_raises_pool_exhausted_and_is_retryable() -> None:
    """取连接超时 ⇒ ``PoolExhausted``，且可重试。

    §9.3 明确要求不能无限等待：无限等待会让整个进程卡在一个看不见的地方，任务既不
    成功也不失败。抛出来上层才能把任务回 ``PENDING`` 稍后重试。
    """
    pool = _FakePool(acquire_delay=0.2)
    db = _db(pool, acquire_timeout_s=0.01)

    with pytest.raises(PoolExhausted) as ei:
        async with db.acquire():
            pass

    assert ei.value.retryable
    assert isinstance(ei.value, PortUnavailable)  # 上层按端口不可用统一处理


async def test_using_the_pool_before_connect_is_a_loud_error() -> None:
    """没 ``connect()`` 就用 ⇒ 直接 ``RuntimeError``。

    返回 None 让调用方拿到一个 ``AttributeError`` 的话，报错点离真正的原因
    （装配顺序错了）隔着好几层。
    """
    db = Database("postgresql://x/y", "alpha")
    with pytest.raises(RuntimeError, match="connect"):
        _ = db.pool


# --- 隔离第 2 层：search_path 与标识符白名单 --------------------------------


def test_search_path_never_contains_public() -> None:
    """``search_path`` 刻意不含 ``public``。

    含了 public，一条漏写 schema 前缀的 SQL 就会静默落到 public——建出一张所有
    业务线共享的表，而且是在**运行期**、没有任何报错。不含它，同一条 SQL 在第一次
    执行时就报"relation does not exist"。
    """
    sp = search_path("alpha")
    assert sp == "ws_alpha, org"
    assert "public" not in sp


@pytest.mark.parametrize(
    "bad",
    [
        "public",  # 保留名
        "org",
        "postgres",
        "pg_catalog",
        "ALPHA",  # 大写
        "1mx",  # 数字开头
        "alpha-1",  # 连字符
        "alpha;DROP SCHEMA org CASCADE",  # 注入
        'alpha" ; --',
        "ws alpha",  # 空格
        "m",  # 太短
        "m" * 32,  # 太长
        "",
        "alpha\n",
    ],
)
def test_identifier_whitelist_rejects_everything_unsafe(bad: str) -> None:
    """标识符白名单：不匹配就抛 ``IsolationViolation``。

    ``schema.py`` 是全仓**唯一**允许把标识符拼进 SQL 文本的地方，所以这个正则是
    那一整类注入的唯一闸门。这里把注入、大小写、长度、保留名都各钉一条：白名单
    被放宽时（比如为了支持某个带连字符的业务线名）必须有用例红。
    """
    with pytest.raises(IsolationViolation):
        safe_identifier(bad)


@pytest.mark.parametrize("ok", ["alpha", "beta", "public_biz", "ws2", "a" * 31])
def test_identifier_whitelist_accepts_legitimate_names(ok: str) -> None:
    """白名单不能严到把正常业务线名也拒掉。

    与上一条成对：只有"坏的被拒"的话，一个恒抛异常的实现也能全绿，而那样连
    ``connect()`` 都跑不起来。
    """
    assert safe_identifier(ok) == ok


def test_schema_and_role_names_derive_from_the_same_whitelist() -> None:
    """schema / role 名都必须过同一个闸门。

    两者中任何一个绕过白名单，注入就有了第二个入口，而 0007 的授权语句同时用到
    这两个名字。
    """
    assert ws_schema("alpha") == "ws_alpha"
    assert ws_role("alpha") == "wetag_ws_alpha"
    for fn in (ws_schema, ws_role):
        with pytest.raises(IsolationViolation):
            fn("alpha; DROP DATABASE wetag")


def test_migration_rendering_substitutes_only_the_two_placeholders() -> None:
    """迁移占位符替换：只有两个，且值都过了白名单。

    checksum 算的是**替换前**的原文，所以同一份文件在不同业务线的 checksum 一致，
    ``init_workspace.py`` 才拦得住"改老文件"。
    """
    sql = "CREATE TABLE @ws_schema@.task (...); GRANT SELECT ON ... TO @ws_role@;"
    got = render_migration(sql, "alpha")
    assert got == "CREATE TABLE ws_alpha.task (...); GRANT SELECT ON ... TO wetag_ws_alpha;"
    assert "@ws_" not in got  # 没有漏替换的占位符

    with pytest.raises(IsolationViolation):
        render_migration(sql, "evil; --")


# --- 隔离第 3 层：语句层与行级护栏 ------------------------------------------


class _StubDb:
    """只提供 Repository 需要的接口，返回预置行。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None, ws: str = "alpha") -> None:
        self.workspace_id = ws
        self._rows = rows or []
        self.sqls: list[str] = []

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        self.sqls.append(sql)
        return list(self._rows)

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.sqls.append(sql)
        return self._rows[0] if self._rows else None

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.sqls.append(sql)
        return None

    async def execute(self, sql: str, *a: Any) -> str:
        self.sqls.append(sql)
        return "OK"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM requirement",
        "SELECT * FROM requirement WHERE tapd_id = $1",
        "UPDATE requirement SET title = $1 WHERE id = $2",
        "DELETE FROM doc_index WHERE id = $1",
    ],
)
async def test_sql_without_workspace_filter_is_refused(sql: str) -> None:
    """不带 ``workspace_id`` 的 SQL ⇒ 拒绝执行。

    权限层（0007）和连接层（search_path）都拦不住 org schema 里的共享表——
    ``chat_ownership`` 是所有业务线都能 SELECT 的。语句层是唯一覆盖这种情况的一层，
    而它的代价只是"每条 SQL 都写上过滤条件"。

    注意 ``DELETE`` 也在列：它不是"反正要删就不必过滤"，恰恰相反。
    """
    repo = Repository(_StubDb())  # type: ignore[arg-type]
    for method in (repo.fetch, repo.fetchrow, repo.fetchval, repo.execute):
        with pytest.raises(IsolationViolation):
            await method(sql)  # type: ignore[operator]


async def test_sql_with_workspace_filter_passes() -> None:
    """带了过滤条件就放行。

    与上一条成对：恒抛异常的实现会让整个 DAO 不可用。
    """
    db = _StubDb()
    repo = Repository(db)  # type: ignore[arg-type]
    await repo.fetch("SELECT * FROM requirement WHERE workspace_id = $1", "alpha")
    assert db.sqls  # 真的执行了


async def test_unfiltered_escape_hatch_is_a_single_greppable_name() -> None:
    """豁免必须走 ``_unfiltered``，且全仓豁免点可一次 grep 完。

    读 ``org.schema_migration`` 这类全局元数据确实不该带过滤条件。关键是豁免要
    **显眼**：名字刻意难看，review 时 grep 一次就能把所有豁免点看完。这条用例顺便
    钉住"豁免点的数量"这件事在人的视野里。
    """
    import subprocess

    repo = Repository(_StubDb())  # type: ignore[arg-type]
    assert repo._unfiltered("SELECT version FROM org.schema_migration").startswith("SELECT")

    out = subprocess.run(
        ["grep", "-rn", "_unfiltered", "app/"], capture_output=True, text=True
    ).stdout
    # 定义处 2 行（def + docstring 内引用）之外的每一处调用都要能被人看见
    call_sites = [l for l in out.splitlines() if "def _unfiltered" not in l]
    assert len(call_sites) <= 12, f"豁免点过多，隔离防线在被逐步掏空：\n{out}"


async def test_a_row_from_another_workspace_raises_instead_of_being_filtered() -> None:
    """读到别人的行 ⇒ 抛异常，**不静默过滤**。

    静默过滤是这里最有害的选项：它让上游过滤失效这件事完全不可见，而下一次可能就
    不是"多读了一行"，而是"写到了别人的表"。抛出来，让它在第一次发生时就停住。
    """
    rows = [{"workspace_id": "alpha", "id": 1}, {"workspace_id": "beta", "id": 2}]
    repo = Repository(_StubDb(rows))  # type: ignore[arg-type]

    with pytest.raises(IsolationViolation) as ei:
        await repo.fetch("SELECT * FROM t WHERE workspace_id = $1", "alpha")

    assert "beta" in str(ei.value)


async def test_rows_without_the_column_pass_through() -> None:
    """不含 ``workspace_id`` 列的结果集（如聚合）正常通过。

    行级校验不能把 ``SELECT count(*)`` 这类查询一并拒掉，否则大家会绕开
    ``Repository`` 直接用 ``db``——那等于三层里少了一层。
    """
    rows = [{"cnt": 7}]
    repo = Repository(_StubDb(rows))  # type: ignore[arg-type]
    got = await repo.fetch("SELECT count(*) AS cnt FROM t WHERE workspace_id = $1", "alpha")
    assert got == rows


def test_labels_normalization_is_deterministic() -> None:
    """``text[]`` 参数去空去重且定序。

    定序不只是为了测试好写：``labels`` 参与 ``dedupe_key`` 的计算，顺序不定会让
    同一条通知在重跑时算出不同的键，于是 ``UNIQUE(workspace_id, dedupe_key)``
    拦不住重复卡片。
    """
    assert Repository.labels_of(["b", "a", "", "b", None]) == ["a", "b"]  # type: ignore[list-item]
    assert Repository.labels_of(None) == []
    assert Repository.labels_of([]) == []
