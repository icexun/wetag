"""端口契约：**真实现与 Fake 必须签名一致**（doc2 §5.1、code-layout §3.4）。

算法层三百多条用例全跑在 Fake 上。这是有意的——它们要断言的是判定逻辑与失败语义，
不该被网络和授权状态绑住。但它换来一个特有的失效模式：**Fake 与真实现悄悄漂移**。
真实现改了参数名、多了一个必填参数、把返回值从 list 改成 tuple，Fake 不改，于是
全部用例继续绿，而生产在第一次真调用时 ``TypeError``。

``runtime_checkable`` 的 ``isinstance`` 挡不住这件事：它**只检查方法名存不存在**，
不看签名。本文件第一条用例就是证明这一点——一个参数全错的类能通过 isinstance。
所以真正的闸门是 ``inspect.signature`` 逐个比对。

覆盖四个端口 × 每个端口的真实现 + 测试里用的 Fake：

* ``DocumentPort``      → ``LarkDocumentAdapter``
* ``RequirementPort``   → ``TapdCliAdapter``
* ``TodoPort``          → ``WecomTodoAdapter``（D-1：唯一实现）
* ``CodeRepositoryPort``→ ``RepoService``
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol, Sequence, get_type_hints

import pytest

from app.capability.integration.document.lark_cli import LarkDocumentAdapter
from app.capability.integration.repo.service import RepoService
from app.capability.integration.requirement.tapd_cli import TapdCliAdapter
from app.capability.integration.todo.wecom import WecomTodoAdapter
from app.contracts.ports import (
    CodeRepositoryPort,
    DocumentPort,
    RequirementPort,
    TodoPort,
)

PORTS = {
    "DocumentPort": DocumentPort,
    "RequirementPort": RequirementPort,
    "TodoPort": TodoPort,
    "CodeRepositoryPort": CodeRepositoryPort,
}

IMPLEMENTATIONS: list[tuple[str, type, type]] = [
    ("DocumentPort", DocumentPort, LarkDocumentAdapter),
    ("RequirementPort", RequirementPort, TapdCliAdapter),
    ("TodoPort", TodoPort, WecomTodoAdapter),
    ("CodeRepositoryPort", CodeRepositoryPort, RepoService),
]


# --- 工具 -------------------------------------------------------------------


def _protocol_methods(proto: type) -> list[str]:
    """Protocol 声明的方法名。排除 dunder 与 Protocol 自身的内务属性。"""
    return sorted(
        name
        for name, val in vars(proto).items()
        if not name.startswith("_") and callable(val)
    )


def _sig(fn: Any) -> inspect.Signature:
    """去掉 ``self`` 后的签名。比对的是**调用方看到的形状**。"""
    sig = inspect.signature(fn)
    params = [p for n, p in sig.parameters.items() if n != "self"]
    return sig.replace(parameters=params)


def _describe(sig: inspect.Signature) -> list[tuple[str, Any, Any]]:
    """把签名压成可读的 (名字, 种类, 默认值) 列表，用于失败时的 diff。"""
    return [
        (p.name, p.kind, "∅" if p.default is inspect.Parameter.empty else p.default)
        for p in sig.parameters.values()
    ]


# --- 为什么 isinstance 不够 -------------------------------------------------


def test_isinstance_alone_does_not_catch_signature_drift() -> None:
    """先证明 ``isinstance`` 挡不住漂移——这是本文件存在的理由。

    ``runtime_checkable`` 的 ``isinstance`` 只看方法名。下面这个类的每个方法参数
    都是错的，返回值类型也是错的，它依然是 ``DocumentPort`` 的"实例"。

    如果契约测试只写 ``assert isinstance(fake, DocumentPort)``，那它等于什么都没测，
    而且会给人一种"契约已经守住了"的错觉——比没有测试更糟。
    """

    class WronglyShaped:
        async def fetch(self, whatever: int) -> str:  # 参数名、类型、返回值全错
            return ""

        async def batch_meta(self) -> None:  # 少了必填参数
            return None

        async def list_tree(self, a: int, b: int, c: int) -> None:  # 多了两个
            return None

        async def update(self) -> None:
            return None

        async def add_comment(self, x: int) -> str:  # 参数、返回值全错
            return ""

    assert isinstance(WronglyShaped(), DocumentPort)  # ← 通过了
    # 而签名比对会把它抓出来
    assert _sig(WronglyShaped.fetch) != _sig(DocumentPort.fetch)


def test_missing_method_is_at_least_caught_by_isinstance() -> None:
    """方法整个缺失还是会被 isinstance 抓到。

    与上一条成对：isinstance 不是没用，只是只覆盖了"名字齐不齐"这一层。两层都要。
    """

    class HalfBaked:
        async def fetch(self, ref: Any, scope: Any) -> Any: ...

    assert not isinstance(HalfBaked(), DocumentPort)


# --- 真实现的签名一致性 -----------------------------------------------------


@pytest.mark.parametrize(
    "port_name,proto,impl",
    IMPLEMENTATIONS,
    ids=[f"{n}:{i.__name__}" for n, _, i in IMPLEMENTATIONS],
)
def test_implementation_signatures_match_the_protocol(
    port_name: str, proto: type, impl: type
) -> None:
    """每个真实现的每个端口方法，签名必须与 Protocol 完全一致。

    "完全一致"包括参数名：Intelligence 层用关键字调用（``fetch(ref=..., scope=...)``）
    是本仓的常见写法，改个参数名就是一次不兼容变更，而它在位置调用的测试里看不出来。
    """
    methods = _protocol_methods(proto)
    assert methods, f"{port_name} 没有声明任何方法，契约是空的"

    for name in methods:
        assert hasattr(impl, name), f"{impl.__name__} 缺少 {name}()"
        want, got = _sig(getattr(proto, name)), _sig(getattr(impl, name))
        assert _describe(got) == _describe(want), (
            f"{impl.__name__}.{name} 签名与 {port_name} 不一致\n"
            f"  期望 {_describe(want)}\n  实际 {_describe(got)}"
        )


@pytest.mark.parametrize(
    "port_name,proto,impl",
    IMPLEMENTATIONS,
    ids=[f"{n}:{i.__name__}" for n, _, i in IMPLEMENTATIONS],
)
def test_every_port_method_is_a_coroutine(
    port_name: str, proto: type, impl: type
) -> None:
    """端口方法必须都是 ``async def``。

    一个同步实现在 ``await`` 它的地方会抛 ``TypeError: object ... can't be used in
    'await' expression``——但只在真的走到那条路径时。而端口方法背后全是网络调用，
    同步实现意味着它在事件循环里阻塞住整个进程（一个 CLI 调用几秒，期间所有业务
    线的任务都停着）。
    """
    for name in _protocol_methods(proto):
        assert inspect.iscoroutinefunction(getattr(impl, name)), (
            f"{impl.__name__}.{name} 不是 async def"
        )


def test_return_annotations_are_resolvable_and_not_any() -> None:
    """端口方法的返回类型必须解析得出，且不是 ``Any``。

    ``-> Any`` 会让整个契约失去意义：任何实现都"符合"。这条同时顺手验证了
    ``from __future__ import annotations`` 下这些注解**确实能解析**——写错一个类型名
    在运行期是静默的，只有在 ``get_type_hints`` 时才现形。
    """
    for port_name, proto in PORTS.items():
        for name in _protocol_methods(proto):
            fn = getattr(proto, name)
            hints = get_type_hints(fn)  # 解析不出来会抛 NameError
            ret = hints.get("return")
            assert ret is not None, f"{port_name}.{name} 没有返回类型注解"
            assert ret is not Any, f"{port_name}.{name} 返回 Any，契约等于没有"


# --- 测试里的 Fake 必须守同一份契约 ------------------------------------------


def _fakes_from_test_modules() -> list[tuple[str, type, type]]:
    """收集各测试模块里"声称实现某端口"的 Fake。

    刻意显式登记而不是自动扫描类名：自动扫描会在有人把 Fake 改名或挪走时**静默
    少测一个**，而那正是漂移发生的时刻。显式列表少一项会在 review 的 diff 里看见。
    """
    import tests.capability.test_workflows as tw

    return [
        ("DocumentPort", DocumentPort, tw.FakeDocuments),
    ]


@pytest.mark.parametrize(
    "port_name,proto,fake",
    _fakes_from_test_modules(),
    ids=[f"{n}:{f.__module__.split('.')[-1]}.{f.__name__}" for n, _, f in _fakes_from_test_modules()],
)
def test_fakes_conform_to_the_same_protocol(
    port_name: str, proto: type, fake: type
) -> None:
    """Fake 的端口方法签名必须与真 Protocol 一致。

    这是本文件的主要价值所在。一旦某个端口改了签名，改动会同时红两处：真实现的
    那条用例（如果实现忘了改）和这里（如果 Fake 忘了改）。没有这条，后者会一直绿。

    只比对 Fake **确实声明了**的端口方法——Fake 按需只实现一部分是合理的。
    缺方法不算漂移，签名不对才算。
    """
    declared = [n for n in _protocol_methods(proto) if n in vars(fake)]
    assert declared, f"{fake.__name__} 没有实现 {port_name} 的任何方法，登记有误"

    for name in declared:
        want, got = _sig(getattr(proto, name)), _sig(getattr(fake, name))
        assert [p[0] for p in _describe(got)] == [p[0] for p in _describe(want)], (
            f"{fake.__name__}.{name} 参数名与 {port_name} 不一致——"
            f"关键字调用会 TypeError\n  期望 {_describe(want)}\n  实际 {_describe(got)}"
        )


# --- Protocol 自身的形状 ----------------------------------------------------


@pytest.mark.parametrize("port_name,proto", list(PORTS.items()))
def test_ports_are_runtime_checkable_protocols(port_name: str, proto: type) -> None:
    """四个端口都必须是 ``@runtime_checkable`` 的 Protocol。

    不是 Protocol 而是基类的话，Fake 就得继承它，测试与实现之间多一条编译期耦合；
    不是 runtime_checkable 的话，连"名字齐不齐"这一层都没有。
    """
    assert issubclass(proto, Protocol)  # type: ignore[arg-type]
    assert getattr(proto, "_is_runtime_protocol", False), (
        f"{port_name} 缺 @runtime_checkable"
    )


def test_protocol_methods_have_no_implementation_bodies() -> None:
    """Protocol 方法体必须是 ``...``，不能偷偷带默认实现。

    带了默认实现，一个忘记实现某方法的适配器就会静默走到那段默认代码上。对端口
    而言"默认行为"几乎总是错的：默认返回空 list 会让"没查到"和"查了没有"合并成
    同一个返回值，正是失败语义诚实要禁止的那件事。
    """
    for port_name, proto in PORTS.items():
        for name in _protocol_methods(proto):
            src = inspect.getsource(getattr(proto, name))
            body = src.split("...")
            assert len(body) >= 2, f"{port_name}.{name} 方法体不是 ..."
            # ... 之后不该还有实质代码
            assert not body[-1].strip().rstrip(":"), f"{port_name}.{name} 带了实现"


def test_sequence_parameters_are_not_list() -> None:
    """批量参数用 ``Sequence`` 而不是 ``list``。

    ``list`` 会让调用方被迫先 ``list(...)`` 一次——而本仓的章节、hit 到处是
    tuple（frozen dataclass 的字段）。这不是洁癖：多一次转换就多一处可能忘记
    的地方，而 ``Sequence`` 两者都接受。
    """
    offenders: list[str] = []
    for port_name, proto in PORTS.items():
        for name in _protocol_methods(proto):
            hints = get_type_hints(getattr(proto, name))
            for pname, ann in hints.items():
                if pname == "return":
                    continue
                if getattr(ann, "__origin__", None) is list:
                    offenders.append(f"{port_name}.{name}({pname})")
    assert offenders == [], f"批量参数应为 Sequence：{offenders}"


def test_sequence_is_actually_used_somewhere() -> None:
    """上一条的配对：确实有方法在用 ``Sequence``。

    只断言"没人用 list"的话，一个所有参数都是 ``str`` 的端口集合也能全绿，那条
    用例就是空的。
    """
    found = False
    for proto in PORTS.values():
        for name in _protocol_methods(proto):
            for pname, ann in get_type_hints(getattr(proto, name)).items():
                if pname != "return" and getattr(ann, "__origin__", None) in (
                    Sequence,
                    __import__("collections.abc", fromlist=["Sequence"]).Sequence,
                ):
                    found = True
    assert found, "没有任何端口方法使用 Sequence，上一条断言是空的"


# --- 端口不得泄漏平台细节 ---------------------------------------------------


def test_ports_do_not_leak_cli_or_db_types() -> None:
    """端口签名里不得出现 ``CliResult`` / asyncpg / MCP 之类的平台类型。

    端口的整个意义是"Intelligence 层不感知协议差异"（doc2 §5.1 开篇）。一旦
    ``CliResult`` 出现在签名里，业务代码就得知道下面是个 CLI；而 R1 的
    回落方案（CLI → MCP）正是要换掉那一层。
    """
    banned = ("CliResult", "Connection", "Record", "Pool", "McpToolCaller", "Popen")
    for port_name, proto in PORTS.items():
        for name in _protocol_methods(proto):
            text = str(inspect.signature(getattr(proto, name)))
            for bad in banned:
                assert bad not in text, f"{port_name}.{name} 泄漏了平台类型 {bad}"


def test_ports_module_does_not_import_platform_layer() -> None:
    """``contracts/ports.py`` 不得 import ``app.platform`` 或 ``app.capability``。

    依赖方向纪律：contracts 是最底层，被所有层 import。它反向依赖任何一层都会
    立刻产生循环 import，而那类问题的报错信息离原因很远。
    """
    src = inspect.getsource(__import__("app.contracts.ports", fromlist=["x"]))
    for bad in ("from app.platform", "import app.platform", "from app.capability"):
        assert bad not in src, f"contracts/ports.py 反向依赖：{bad}"
