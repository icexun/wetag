"""装配容器的测试。

这里守的是一件事：**不可用的能力必须以"不可用"的形态存在**，不能变成一个
安静返回空结果的东西。所以用例大多在断言"某个字段是 None 且有理由"，而不是
在断言功能。

另外钉住三条今日已知缺口（R1/R2/R4）的表现形式——它们是要在 Demo 里承认的东西，
所以必须能从容器里读出来，而不是散落在日志里。
"""


from types import SimpleNamespace
from typing import Any

import pytest

from app.container import PortStatus, build_capability
from app.contracts.errors import PortUnavailable


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉可能从开发机环境漏进来的配置，让用例结果与本机状态无关。"""
    for key in (
        "PG_DSN",
        "REPO_ROOT",
        "TAPD_PROJECT_ID",
    "LARK_CLI_TOKEN",
    "TAPD_TOKEN",
    "WECOM_CLI_TOKEN",
    "AUTO_APPROVE_RULES",
):
        monkeypatch.delenv(key, raising=False)


async def _build(**kw):
    return await build_capability("alpha", probe=True, env={}, **kw)


# --- 不可用 = None + 理由 ---------------------------------------------------


async def test_unavailable_ports_are_none_with_reason() -> None:
    """没配 PG / REPO_ROOT / 没装 wecom-cli → 端口为 None，各带一条理由。

    这条用例的价值不在"是 None"，在**有理由**：回复里要写"代码搜索没跑，因为
    REPO_ROOT 未配置"，而不是"没找到"。
    """
    c = await _build()
    assert c.db is None
    assert c.code is None

    # 本机装了 wecom-cli 时 todos 真可用，不可用断言只在没装的机器上成立
    todos_unavailable = not c.status("todos").available
    if todos_unavailable:
        assert c.todos is None

    for name, keyword in (
        ("db", "PG_DSN"),
        ("code", "REPO_ROOT"),
        ("todos", "wecom-cli"),
    ):
        if name == "todos" and not todos_unavailable:
            continue
        st = c.status(name)
        assert st.available is False
        assert keyword in st.reason, (name, st.reason)


async def test_require_raises_with_port_and_reason() -> None:
    """``require`` 把"忘记判 None"变成带端口名与原因的异常。

    否则它表现为 ``AttributeError: 'NoneType' object has no attribute 'candidates'``
    ——日志里看不出是哪条外部依赖没起来。
    """
    c = await _build()
    with pytest.raises(PortUnavailable) as err:
        c.require("code")
    assert err.value.port == "code"
    assert "REPO_ROOT" in err.value.reason


# --- 覆盖范围（§9.3）-------------------------------------------------------


async def test_coverage_note_is_nonempty_today() -> None:
    """今日必然非空——它为空才是"没有变化"可信的前提。"""
    c = await _build()
    note = c.coverage_note()
    assert note
    # 本机装了 wecom-cli 时 R2 缺口不存在，这条断言只在没装的机器上有意义
    if c.status("todos").available:
        pytest.skip("本机装了 wecom-cli → R2 今日缺口不存在")
    assert "R2" in note or "wecom-cli" in note


async def test_degraded_but_available_still_shows_in_coverage() -> None:
    """可用但有保留也要进覆盖范围，不能因为端口可用就把保留咽下去。"""
    st = PortStatus("requirements", True, impl="tapd_cli", reason="只读")
    assert st.degraded is True
    assert "但 只读" in st.render()

    clean = PortStatus("documents", True, impl="lark_cli")
    assert clean.degraded is False


# --- 需求口：官方 tapd-cli，无替身 -----------------------------------------


async def test_requirements_unavailable_without_token() -> None:
    """没配 TAPD_TOKEN → 不起探测，端口不可用并点明原因。"""
    c = await build_capability("alpha", probe=True, env={})
    st = c.status("requirements")
    assert st.available is False
    assert "TAPD_TOKEN" in st.reason
    assert c.requirements is None


async def test_requirements_wired_when_probe_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.capability.integration.requirement.tapd_cli import TapdCliAdapter

    async def ok(self) -> bool:
        return True

    monkeypatch.setattr(TapdCliAdapter, "probe", ok)
    c = await build_capability("alpha", probe=True, env={"TAPD_TOKEN": "t"})
    st = c.status("requirements")
    assert st.available is True and st.impl == "tapd_cli"
    assert isinstance(c.requirements, TapdCliAdapter)


async def test_requirements_probe_failure_reason_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.capability.integration.requirement.tapd_cli import TapdCliAdapter

    async def bad(self) -> bool:
        self.probe_error = "TAPD 鉴权失败：token invalid"
        return False

    monkeypatch.setattr(TapdCliAdapter, "probe", bad)
    c = await build_capability("alpha", probe=True, env={"TAPD_TOKEN": "t"})
    st = c.status("requirements")
    assert st.available is False
    assert "鉴权失败" in st.reason
    assert c.requirements is None


async def test_disabled_integrations_skip_their_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """只启用 wecom：需求口、文档口不装配也不探测，理由点明是开关关掉的。"""
    from app.capability.integration.requirement.tapd_cli import TapdCliAdapter

    async def boom(self) -> bool:  # noqa: ANN001
        raise AssertionError("未启用的集成不该被探测")

    monkeypatch.setattr(TapdCliAdapter, "probe", boom)
    c = await build_capability(
        "alpha", probe=True, env={"INTEGRATIONS": '["wecom"]', "TAPD_TOKEN": "t"}
    )
    assert c.workspace.integrations == frozenset({"wecom"})
    for port in ("requirements", "documents"):
        st = c.status(port)
        assert st.available is False and "未启用" in st.reason
    assert c.requirements is None and c.documents is None
    assert "未启用" not in c.status("todos").reason


# --- R4：代码口宁缺毋滥 -----------------------------------------------------


async def test_repo_root_pointing_nowhere_disables_code_dimension() -> None:
    c = await build_capability(
        "alpha", probe=True, env={"REPO_ROOT": "/definitely/not/a/dir/xyz"}
    )
    assert c.code is None
    assert "不是目录" in c.status("code").reason


async def test_repo_root_never_defaults_to_cwd() -> None:
    """``REPO_ROOT`` 未配时**不得**退化成搜当前目录。

    那样代码搜索会在一个与线上代码无关的仓库里搜，产出的 locator 语法完全合法、
    文件真实存在，而结论与线上行为毫无关系——这种证据比没有证据坏，因为它看起来
    可信。
    """
    c = await _build()
    assert c.code is None


async def test_code_dimension_enabled_with_real_repo(tmp_path) -> None:
    """指向一个真目录 → 代码口装配成功（R4 的临时方案：先指向本仓）。"""
    c = await build_capability("alpha", probe=True, env={"REPO_ROOT": str(tmp_path)})
    st = c.status("code")
    if not st.available:
        # 本机没有 rg 也没有 git 时的合法结果
        assert "rg" in st.reason
        return
    assert c.code is not None
    assert st.impl == "repo_service"


# --- env 覆盖的边界 ---------------------------------------------------------


async def test_explicit_env_is_the_whole_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式传 ``env`` 时，进程环境里的同名键**不得**漏进来。

    这条护栏的由来：``Value`` 描述符每次访问都直接读 ``os.environ``，所以
    ``env={}`` 曾经只清掉凭据，而 ``PG_DSN`` / ``REPO_ROOT`` 照旧从开发机漏进容器。
    后果不是"某条用例偶尔红"，是上面那批断言"能力不可用"的用例在配了环境的人
    机器上**静默变成断言另一回事**——它们仍然绿，但守的东西没了。
    """
    monkeypatch.setenv("PG_DSN", "postgresql://leaked/from/dev/machine")
    monkeypatch.setenv("REPO_ROOT", "/tmp")

    c = await build_capability("alpha", probe=True, env={})
    assert c.db is None
    assert "PG_DSN 未配置" in c.status("db").reason
    assert c.code is None
    assert "未配置" in c.status("code").reason

    # env 里给了就要用——覆盖不是"一律当空"
    c2 = await build_capability("alpha", probe=True, env={"REPO_ROOT": "/tmp"})
    assert "未配置" not in c2.status("code").reason


# --- 凭据 -------------------------------------------------------------------


async def test_secret_values_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """装配日志只打凭据的**键名**，不打值。"""
    caplog.set_level("INFO")
    await build_capability("alpha", probe=True, env={"LARK_CLI_TOKEN": "s3cret-value-xyz"})
    text = caplog.text
    assert "LARK_CLI_TOKEN" in text
    assert "s3cret-value-xyz" not in text


# --- 语义节点的超时 ---------------------------------------------------------


async def test_the_configured_timeout_reaches_the_semantic_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LLM_TIMEOUT_S`` 必须真的传到结构化客户端上。

    这条值得单独钉，因为配置没接上的表现**不是**报错：客户端会用自己的默认值 90 秒
    继续跑，而需求结构化一次要回十几个字段外加验收标准，实测跑不完。跑不完的结果是
    ``ok=False`` → 整条检测 ``completed=False``——能力看着装配成功，却什么结论都给不出来。
    """
    monkeypatch.setenv("LLM_TIMEOUT_S", "240")
    c = await _build()
    assert c.agent is not None, c.status("agent").reason
    assert c.agent._timeout_s == 240.0


async def test_a_missing_timeout_config_does_not_fall_back_to_ninety_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配就用 180 秒，不是客户端那个 90 秒。

    ``float(cap.llm_timeout_s or 180.0)`` 里的 ``or`` 看着多余——``Value`` 有 default。
    它挡的是配置中心把这一项下发成空串或 0 的情况：0 秒超时等于每次调用立刻失败，
    而症状和"网关挂了"一模一样。
    """
    monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
    c = await _build()
    assert c.agent is not None, c.status("agent").reason
    assert c.agent._timeout_s == 180.0


async def test_a_zero_timeout_from_config_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下发成 0 的超时不能被当成"0 秒"。

    ``asyncio.wait_for(..., 0)`` 会立刻抛超时，于是**每一个**语义节点都失败，
    而日志上看起来就是网关不可用——排查方向被整个带偏。
    """
    monkeypatch.setenv("LLM_TIMEOUT_S", "0")
    c = await _build()
    assert c.agent is not None, c.status("agent").reason
    assert c.agent._timeout_s == 180.0


# --- 语义节点接线 -----------------------------------------------------------


def _st(statuses: list[PortStatus], name: str) -> PortStatus:
    for s in statuses:
        if s.name == name:
            return s
    raise AssertionError(f"没有记 {name} 的 PortStatus——不可用也必须留下理由")


def _bare_container() -> Any:
    """只为跑 ``_wire_intelligence`` 的空壳容器。

    刻意用真的 ``CapabilityContainer``（``slots=True``）而不是 ``SimpleNamespace``：
    装配往容器上写一个没声明的字段时，前者当场 ``AttributeError``，后者会安静地
    长出一个没人读的属性。
    """
    from app.container import CapabilityContainer

    return CapabilityContainer(workspace=object(), cli=object())  # type: ignore[arg-type]


def _fake_cap() -> Any:
    """只提供 ``_wire_intelligence`` 读到的那几个配置。"""
    return SimpleNamespace(embedding_model=None, embedding_dim=1024, embedding_timeout_s=20.0)


def test_embedder_falls_back_to_lexical_and_says_so() -> None:
    """没配 ``EMBEDDING_MODEL`` ⇒ 退到词法向量，且 ``impl`` 写明不是语义向量。"""
    from app.container import _wire_intelligence

    c = _bare_container()
    statuses: list[PortStatus] = []

    _wire_intelligence(c, statuses, _fake_cap())

    assert c.embedder is not None
    st = _st(statuses, "embedder")
    assert st.available is True
    assert st.impl == "lexical_ngram"


def test_semantic_nodes_need_an_agent() -> None:
    from app.container import _wire_intelligence

    c = _bare_container()
    _wire_intelligence(c, [], _fake_cap())
    assert c.structuring is None and c.topic_classifier is None

    c = _bare_container()
    c.agent = object()
    _wire_intelligence(c, [], _fake_cap())
    assert c.structuring is not None and c.topic_classifier is not None


# --------------------------------------------------------------------------
# T049 依赖装配容器验收。
# --------------------------------------------------------------------------


import asyncpg
import pytest

from app.container import Container, NotProvidedPort, bind_ports, jobstore_url_for
from app.platform.db.migration_check import (
    SchemaChecksumMismatch,
    SchemaVersionMismatch,
)
from migrations.version import EXPECTED_SCHEMA_VERSION, WORKSPACE_MIGRATIONS


async def _save_ledger(conn):
    return await conn.fetch("SELECT * FROM org.schema_migration")


async def _restore_ledger(conn, saved):
    await conn.execute("DELETE FROM org.schema_migration")
    for row in saved:
        await conn.execute(
            """
            INSERT INTO org.schema_migration
                   (filename, version, scope, workspace_id, checksum, applied_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            row["filename"], row["version"], row["scope"],
            row["workspace_id"], row["checksum"], row["applied_at"],
        )


async def _set_ledger_version(conn, version: int) -> None:
    await conn.execute("DELETE FROM org.schema_migration")
    await conn.execute(
        """
        INSERT INTO org.schema_migration
               (filename, version, scope, workspace_id, checksum)
        VALUES ($1, $2, 'workspace', '', 'x')
        """,
        f"{version:04d}_test.sql", version,
    )


@pytest.fixture()
async def org_version_ready(admin_dsn, org_ready):
    """固定版本台账为期望版本，结束后恢复现场（``org.schema_migration`` 跨测试共享）。"""
    conn = await asyncpg.connect(admin_dsn)
    saved = await _save_ledger(conn)
    await _set_ledger_version(conn, EXPECTED_SCHEMA_VERSION)
    await conn.close()
    yield
    conn = await asyncpg.connect(admin_dsn)
    await _restore_ledger(conn, saved)
    await conn.close()


async def test_build_assembles_all_components(pg_schema, org_version_ready):
    container = await Container.build(
        pg_schema.workspace, dsn=pg_schema.admin_dsn, pool_min_size=1, pool_max_size=2
    )
    try:
        assert container.workspace_id == pg_schema.workspace
        # 全部 Job + approval_expiry 补注册 + 会话任务 handler
        names = {j.name for j in container.registry.all()}
        assert {
            "doc_poll", "tapd_sync", "repo_sync", "todo_readback",
            "index_refresh", "loop_digest",
            "lease_reaper", "approval_expiry",
        } <= names
        assert "agent_reply" in container.worker_pool._handlers
        # 缺失的外部 Port 全部显式占位
        for name in ("document", "requirement", "todo", "repo"):
            assert isinstance(container.ports[name], NotProvidedPort)
        # 调度器已按注册表绑定常驻 job
        assert container.scheduler.get_job("doc_poll") is not None
    finally:
        await container.close()


async def test_not_provided_port_raises_on_use(pg_schema, org_version_ready):
    container = await Container.build(
        pg_schema.workspace, dsn=pg_schema.admin_dsn, pool_min_size=1, pool_max_size=2
    )
    try:
        with pytest.raises(PortUnavailable, match="document"):
            container.ports["document"].list_meta
    finally:
        await container.close()


def test_bind_ports_passes_through_provided():
    provided = {"document": object(), "capability:x": object()}
    bound = bind_ports(provided)
    assert bound["document"] is provided["document"]
    assert bound["capability:x"] is provided["capability:x"]
    assert isinstance(bound["todo"], NotProvidedPort)


async def test_missing_workspace_id_refused(monkeypatch, pg_schema, org_version_ready):
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    with pytest.raises(ValueError, match="WORKSPACE_ID"):
        await Container.build(None, dsn=pg_schema.admin_dsn)


async def test_missing_dsn_refused(monkeypatch, pg_schema, org_version_ready):
    monkeypatch.delenv("PG_DSN", raising=False)
    with pytest.raises(ValueError, match="PG_DSN"):
        await Container.build(pg_schema.workspace, dsn=None)


async def test_schema_drift_refused(pg_schema, admin_dsn, org_ready):
    """库内版本落后于期望版本：拒绝装配并输出差异（US1-4）。"""
    conn = await asyncpg.connect(admin_dsn)
    saved = await _save_ledger(conn)
    await _set_ledger_version(conn, EXPECTED_SCHEMA_VERSION - 1)
    await conn.close()
    try:
        with pytest.raises(SchemaVersionMismatch):
            await Container.build(
                pg_schema.workspace, dsn=pg_schema.admin_dsn,
                pool_min_size=1, pool_max_size=2,
            )
    finally:
        conn = await asyncpg.connect(admin_dsn)
        await _restore_ledger(conn, saved)
        await conn.close()


async def test_schema_checksum_drift_refused(pg_schema, admin_dsn, org_ready):
    """版本号对得上、但迁移文件内容变过：同样拒绝装配。

    这正是 ws_alpha 漏掉的那种漂移——0002 被就地重写，序号仍是 2，只比
    ``max(version)`` 一路绿灯，库里却躺着老形状的表，直到第一次查询才炸在
    ``column "priority" does not exist``。只比版本号发现不了，所以必须比内容。
    """
    conn = await asyncpg.connect(admin_dsn)
    saved = await _save_ledger(conn)
    await _set_ledger_version(conn, EXPECTED_SCHEMA_VERSION)
    # 本业务线记一行 workspace 迁移，checksum 故意与磁盘文件不符
    await conn.execute(
        """
        INSERT INTO org.schema_migration
               (filename, version, scope, workspace_id, checksum)
        VALUES ($1, 2, 'workspace', $2, 'deadbeef')
        """,
        WORKSPACE_MIGRATIONS[0],
        pg_schema.workspace,
    )
    await conn.close()
    try:
        with pytest.raises(SchemaChecksumMismatch) as excinfo:
            await Container.build(
                pg_schema.workspace, dsn=pg_schema.admin_dsn,
                pool_min_size=1, pool_max_size=2,
            )
        # 报错要指名道姓是哪个文件，否则运维无从下手
        assert WORKSPACE_MIGRATIONS[0] in str(excinfo.value)
    finally:
        conn = await asyncpg.connect(admin_dsn)
        await _restore_ledger(conn, saved)
        await conn.close()


async def test_other_workspace_drift_does_not_block_startup(
    pg_schema, admin_dsn, org_ready
):
    """同库里**别条**业务线漂移，不拦本进程启动。

    校验按 workspace_id 分片：一条业务线的历史包袱不该把其余进程一起按住。
    """
    conn = await asyncpg.connect(admin_dsn)
    saved = await _save_ledger(conn)
    await _set_ledger_version(conn, EXPECTED_SCHEMA_VERSION)
    await conn.execute(
        """
        INSERT INTO org.schema_migration
               (filename, version, scope, workspace_id, checksum)
        VALUES ($1, 2, 'workspace', 'some_other_ws', 'deadbeef')
        """,
        WORKSPACE_MIGRATIONS[0],
    )
    await conn.close()
    try:
        container = await Container.build(
            pg_schema.workspace, dsn=pg_schema.admin_dsn,
            pool_min_size=1, pool_max_size=2,
        )
        await container.close()
    finally:
        conn = await asyncpg.connect(admin_dsn)
        await _restore_ledger(conn, saved)
        await conn.close()


def test_jobstore_url_derivation():
    assert jobstore_url_for("postgresql://u:p@host:5432/db") == (
        "postgresql+psycopg://u:p@host:5432/db"
    )
    with pytest.raises(ValueError):
        jobstore_url_for("sqlite:///x.db")


async def test_auto_approve_rules_wired_from_env(
    monkeypatch: pytest.MonkeyPatch, pg_schema, org_version_ready
):
    """``AUTO_APPROVE_RULES`` 经 ControlProperties 落进审批闸门（US7-4）。

    配置没接上的表现不是报错，是规则静默不生效、审批卡照发——所以这条要钉。
    """
    import json

    monkeypatch.setenv(
        "AUTO_APPROVE_RULES",
        json.dumps(
            [{
                "tool": "feishu_doc_update",
                "params": {"doc_url": r"^https://example\.feishu\.cn/wiki/JRxV.*"},
            }]
        ),
    )
    container = await Container.build(
        pg_schema.workspace, dsn=pg_schema.admin_dsn, pool_min_size=1, pool_max_size=2
    )
    try:
        matcher = container.agent_runner._gate._auto
        hit = matcher.match(
            "feishu_doc_update",
            {"command": "str_replace",
             "doc_url": "https://example.feishu.cn/wiki/JRxVWikiDemoToken0001"},
        )
        assert hit.hit is True  # 圈内文档免审批直通
        miss = matcher.match(
            "feishu_doc_update",
            {"command": "str_replace", "doc_url": "https://other.example.com/wiki/x"},
        )
        assert miss.hit is False  # 圈外仍走人工审批
    finally:
        await container.close()
